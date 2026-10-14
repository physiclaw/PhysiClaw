"""Tests for `physiclaw.agent.claude.session_log` — the claude wake's
daily narrative + per-session artifact dir. Extracted 1:1 from
test_spawn.py alongside the source split."""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from physiclaw.agent.claude import session_log
from physiclaw.agent.claude.session_log import _redact_images, _SessionLog
from physiclaw.agent.runtime.hook import Trigger

# Mutation audit: accept narrative wording, missing-field placeholders, diagnostic
# traceback detail and one-character truncation changes. Also equivalent: zero
# cost comparisons, falsy initial state, overwritten fallback recap, unknown MIME
# falling back to JPEG, unknown event defaults, and unevaluated annotations.
# Persisted metrics, actual event content, image tags and outcomes are asserted.

# ---------- _redact_images ----------


def test_redact_images_passes_through_non_list() -> None:
    assert _redact_images("just text") == "just text"
    assert _redact_images(None) is None


def test_redact_images_replaces_image_data_with_placeholder() -> None:
    content = [
        {"type": "text", "text": "hi"},
        {"type": "image", "source": {"data": "AAAA", "media_type": "png"}},
    ]

    out = _redact_images(content)

    assert out[0] == {"type": "text", "text": "hi"}
    assert out[1]["source"]["data"] == "<4b elided>"
    assert out[1]["source"]["media_type"] == "png"


def test_redact_images_image_with_no_source() -> None:
    content = [{"type": "image"}]

    out = _redact_images(content)

    assert out[0]["source"] == {"data": "<0b elided>"}


# ---------- _SessionLog ----------


@pytest.fixture
def _isolated_log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(session_log, "LOG_DIR", log_dir)
    return log_dir


def _slog(sources: list[str]) -> _SessionLog:
    """Build a `_SessionLog` from bare source strings. Fixed sid/model/prompt
    so the artifact dir is deterministic; the session dir lands under the
    autouse `physiclaw_home` tmp via `paths.claude_sessions_dir()`."""
    triggers = [Trigger(source=s, description=s) for s in sources]
    return _SessionLog(
        "20260101_120000_test00",
        triggers,
        model_ref="claude-code/test-model",
        prompt_hash="0" * 64,
    )


def _read_summary():
    from physiclaw.common import paths

    return json.loads(
        (
            paths.claude_sessions_dir() / "20260101_120000_test00" / "summary.json"
        ).read_text()
    )


def _feed(slog, events):
    try:
        for event in events:
            slog.event(event)
    finally:
        slog.close()


def _tool_result(content, **fields):
    return {"type": "tool_result", "content": content, **fields}


def _image_source(mime):
    return {"type": "base64", "media_type": mime, "data": "YQ=="}


@pytest.mark.parametrize(
    "value",
    [None, True, False, -1, [], {}, "bad", float("nan"), float("inf"), -float("inf")],
)
def test_invalid_usage_and_cost_default_to_zero(_isolated_log_dir, value):
    slog = _slog([])
    fields = [
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    ]

    _feed(
        slog,
        [
            {
                "type": "result",
                "usage": dict.fromkeys(fields, value),
                "total_cost_usd": value,
                "duration_api_ms": value,
            }
        ],
    )

    summary = _read_summary()
    assert summary["usage"] == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_hit_pct": 0,
        "new_tokens": 0,
    }
    assert summary["cost_usd"] == 0.0
    assert summary["provider_time_ms"] == 0


def test_summary_before_result_keeps_session_identity_and_zero_counters(
    _isolated_log_dir,
):
    from freezegun import freeze_time

    from physiclaw.common import paths

    with freeze_time("2026-01-01 12:00:00") as clock:
        slog = _slog(["phone"])
        clock.tick(2.5)

        slog.close()

    summary = _read_summary()
    assert summary["prompt_hash"] == "0" * 64
    assert summary["started_at"].startswith("2026-01-01T12:00:00")
    assert summary["duration_s"] == 2.5
    assert summary["triggers"] == [{"source": "phone", "description": "phone"}]
    assert summary["outcome"] == {"sentinel": None, "recap": "", "crashed": False}
    assert (
        summary["provider_time_ms"]
        == summary["cost_usd"]
        == summary["stuck_events"]
        == 0
    )
    assert summary["errors"]["tool_errors"] == 0
    assert isinstance(summary["env"], dict)
    assert (paths.claude_sessions_dir() / "README.md").is_file()


def test_summary_counts_repeated_tools_errors_and_cache_creation(_isolated_log_dir):
    slog = _slog([])
    calls = [{"type": "tool_use", "name": []}] * 2
    results = [_tool_result("failed", is_error=True)] * 2
    results += [_tool_result("ok"), {"type": "text", "is_error": True}]

    _feed(
        slog,
        [
            {"type": "assistant", "message": {"content": calls}},
            {"type": "user", "message": {"content": results}},
            {
                "type": "result",
                "usage": {"input_tokens": 10, "cache_creation_input_tokens": 20},
            },
        ],
    )

    summary = _read_summary()
    assert summary["tool_calls"] == {"?": 2}
    assert summary["errors"]["tool_errors"] == 2
    assert summary["usage"]["cache_creation_tokens"] == 20
    assert summary["usage"]["input_tokens"] == 30


@pytest.mark.parametrize("with_ids", [True, False])
def test_images_skip_unrelated_blocks_and_advance_turn_tags(
    _isolated_log_dir, with_ids
):
    from physiclaw.common import paths

    slog = _slog([])
    assistant = [
        {"type": "assistant", "message": {"id": str(n) if with_ids else None}}
        for n in range(3)
    ]
    content = [
        "noise",
        {"type": "text", "text": "noise"},
        {"type": "image", "source": _image_source("image/png")},
        {"type": "image", "source": _image_source("image/jpeg")},
        {"type": "image", "source": {"type": "url", "data": "YQ=="}},
    ]
    user = {
        "type": "user",
        "message": {
            "content": [
                {"type": "text"},
                _tool_result("not a list"),
                _tool_result(content),
            ]
        },
    }

    _feed(slog, [*assistant, user])

    images = paths.claude_sessions_dir() / "20260101_120000_test00" / "images"
    assert _read_summary()["images"] == 2
    assert {p.suffix for p in images.iterdir()} == {".png", ".jpg"}
    assert all("_t3" in p.name for p in images.iterdir())


def test_daily_narrative_preserves_content_for_each_event_kind(_isolated_log_dir):
    slog = _slog([])
    blocks = [
        {"type": "text", "text": "block-message"},
        {"type": "thinking", "thinking": "thought-message"},
        {"type": "tool_use", "name": "peek", "input": {"note": "tool-input-needle"}},
        {"type": "unknown", "text": "wrong-text", "thinking": "wrong-thought"},
    ]

    _feed(
        slog,
        [
            {"type": "assistant", "message": {"content": "plain-message"}},
            {"type": "assistant", "message": {"content": blocks}},
            {"type": "user", "message": {"content": [_tool_result("result-needle")]}},
        ],
    )

    text = next(_isolated_log_dir.glob("claude-*.log")).read_text()
    assert all(
        value in text
        for value in [
            "plain-message",
            "block-message",
            "thought-message",
            "tool-input-needle",
            "result-needle",
        ]
    )
    assert "wrong-text" not in text and "wrong-thought" not in text


def test_session_start_purges_only_expired_claude_logs(_isolated_log_dir, mocker):
    import os

    _isolated_log_dir.mkdir()
    old = _isolated_log_dir / "claude-2000-01-01.log"
    other = _isolated_log_dir / "engine-2000-01-01.log"
    old.write_text("old")
    other.write_text("other")
    os.utime(old, (0, 0))
    os.utime(other, (0, 0))
    mocker.patch.object(session_log.CONFIG.retention, "log_days", 7)

    slog = _slog([])
    slog.close()

    assert not old.exists()
    assert other.read_text() == "other"


def test_session_log_survives_hostile_event_shapes(_isolated_log_dir: Path) -> None:
    """The `claude` CLI is external and unpinned — shape drift must cost
    a log line, never the wake: no exception may escape event(), and a
    string-shaped assistant content still carries the sentinel."""
    slog = _slog([])
    hostile: list[dict] = [
        {"type": "assistant", "message": None},
        {"type": "assistant", "message": "plain string"},
        {
            "type": "assistant",
            "message": {"id": "m1", "content": "string content\n>> DONE - ok"},
        },
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use"}, "stray-string", 42]},
        },
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "is_error": True}, None]},
        },
        {"type": "user", "message": {"content": "not-a-list"}},
    ]
    for event in hostile:
        slog.event(event)

    assert slog.done(0) == "DONE"  # the string-content sentinel was captured
    slog.close()


def test_session_log_init_writes_wake_header(_isolated_log_dir: Path) -> None:
    slog = _slog(["cron:a", "phone"])
    slog.close()

    files = list(_isolated_log_dir.glob("claude-*.log"))
    assert len(files) == 1
    text = files[0].read_text()
    assert "WAKE session=20260101_120000_test00 triggers=['cron:a', 'phone']" in text
    assert "=" * 60 in text


def test_session_log_writes_summary_json(_isolated_log_dir: Path) -> None:

    from physiclaw.common import paths

    slog = _slog(["phone"])
    # Two assistant events sharing ONE message id — the content-block streaming
    # of a single API response. Must count as ONE provider call, not two, and
    # their (partial, repeated) usage must NOT be summed.
    slog.event(
        {
            "type": "assistant",
            "message": {
                "id": "msg_1",
                "content": [{"type": "tool_use", "name": "peek", "input": {}}],
                "usage": {"input_tokens": 100, "output_tokens": 4},
            },
        }
    )
    slog.event(
        {
            "type": "assistant",
            "message": {
                "id": "msg_1",
                "content": [{"type": "text", "text": ">> DONE - bought it"}],
                "usage": {"input_tokens": 100, "output_tokens": 4},
            },
        }
    )
    # The result event is the authoritative cumulative token/cost/time record.
    slog.event(
        {
            "type": "result",
            "num_turns": 1,
            "total_cost_usd": 0.0123,
            "duration_api_ms": 4200,
            "usage": {
                "input_tokens": 100,
                "output_tokens": 30,
                "cache_read_input_tokens": 400,
                "cache_creation_input_tokens": 0,
            },
        }
    )
    slog.done(0)
    slog.close()

    s = json.loads(
        (
            paths.claude_sessions_dir() / "20260101_120000_test00" / "summary.json"
        ).read_text()
    )
    assert s["schema"] == 1
    assert s["provider"] == "claude-code"
    assert s["model_ref"] == "claude-code/test-model"
    assert s["outcome"] == {"sentinel": "DONE", "recap": "bought it", "crashed": False}
    assert s["turns"] == 1  # one distinct message, despite two stream events
    assert s["provider_calls"] == 1
    assert s["tool_calls"] == {"peek": 1}
    # Tokens from result.usage (authoritative), not summed per-assistant.
    assert s["usage"]["input_tokens"] == 100 + 400  # input + cache_read
    assert s["usage"]["output_tokens"] == 30
    assert s["usage"]["cache_read_tokens"] == 400
    assert s["cost_usd"] == 0.0123
    assert s["provider_time_ms"] == 4200
    # Durable copy at the sweep-free `log/` root, shared with the engine:
    # one summary line per session, surviving session-dir retention.
    stats = (paths.LOG_DIR / "stats.jsonl").read_text().splitlines()
    assert json.loads(stats[0])["sid"] == "20260101_120000_test00"
    # Engine-only fields are DROPPED, not faked as zero — the claude
    # stream carries no per-tool timing or camera verdicts.
    assert "tool_time_ms" not in json.loads(stats[0])


def test_session_log_extracts_screenshot(_isolated_log_dir: Path) -> None:
    import base64

    from physiclaw.common import paths

    slog = _slog(["phone"])
    slog.event(
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "peek", "input": {}}]},
        }
    )
    b64 = base64.b64encode(b"\xff\xd8fake-jpeg").decode()
    slog.event(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/jpeg",
                                    "data": b64,
                                },
                            },
                            {"type": "text", "text": "elements..."},
                        ],
                    }
                ]
            },
        }
    )
    slog.close()

    imgs = list(
        (paths.claude_sessions_dir() / "20260101_120000_test00" / "images").glob("*")
    )
    assert len(imgs) == 1
    assert imgs[0].name.endswith("_t1.jpg")  # turn 1 (one assistant seen)
    assert imgs[0].read_bytes() == b"\xff\xd8fake-jpeg"


def test_session_log_image_turn_tag_dedups_streamed_message(
    _isolated_log_dir: Path,
) -> None:
    import base64

    from physiclaw.common import paths

    slog = _slog(["phone"])
    # One assistant MESSAGE streamed as two events sharing message.id —
    # the turn must advance ONCE, so the screenshot tags as t1, not t2.
    for block in (
        {"type": "text", "text": "looking"},
        {"type": "tool_use", "name": "peek", "input": {}},
    ):
        slog.event(
            {"type": "assistant", "message": {"id": "msg_1", "content": [block]}}
        )
    b64 = base64.b64encode(b"\xff\xd8fake-jpeg").decode()
    slog.event(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/jpeg",
                                    "data": b64,
                                },
                            }
                        ],
                    }
                ]
            },
        }
    )
    slog.close()

    imgs = list(
        (paths.claude_sessions_dir() / "20260101_120000_test00" / "images").glob("*")
    )
    assert len(imgs) == 1
    assert imgs[0].name.endswith("_t1.jpg")  # one message despite two events


def test_session_log_event_assistant_text_returns_none(
    _isolated_log_dir: Path,
) -> None:
    slog = _slog([])
    out = slog.event(
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "hello"}]},
        }
    )
    slog.close()

    assert out is None


def test_session_log_event_result_returns_data(_isolated_log_dir: Path) -> None:
    slog = _slog([])
    data = {"type": "result", "num_turns": 5, "result": "all done"}
    out = slog.event(data)
    slog.close()

    assert out == data


def test_session_log_summarizes_tool_use(_isolated_log_dir: Path) -> None:
    slog = _slog([])
    slog.event(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Read", "input": {"path": "x"}},
                ]
            },
        }
    )
    slog.close()

    text = list(_isolated_log_dir.glob("claude-*.log"))[0].read_text()
    assert "tool_use: Read" in text


def test_session_log_summarizes_thinking(_isolated_log_dir: Path) -> None:
    slog = _slog([])
    slog.event(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "let me think"},
                ]
            },
        }
    )
    slog.close()

    text = list(_isolated_log_dir.glob("claude-*.log"))[0].read_text()
    assert "thinking: let me think" in text


def test_session_log_user_event_without_tool_result_no_summary(
    _isolated_log_dir: Path,
) -> None:
    slog = _slog([])
    slog.event(
        {
            "type": "user",
            "message": {"content": [{"type": "text", "text": "ack"}]},
        }
    )
    slog.close()

    # No crash; no tool_result line written.
    text = list(_isolated_log_dir.glob("claude-*.log"))[0].read_text()
    assert "tool_result:" not in text


def test_session_log_summarizes_user_tool_result(_isolated_log_dir: Path) -> None:
    slog = _slog([])
    slog.event(
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "content": "result value"},
                ]
            },
        }
    )
    slog.close()

    text = list(_isolated_log_dir.glob("claude-*.log"))[0].read_text()
    assert "tool_result:" in text


def test_session_log_summarizes_result(_isolated_log_dir: Path) -> None:
    slog = _slog([])
    slog.event({"type": "result", "num_turns": 3, "result": "ok"})
    slog.close()

    text = list(_isolated_log_dir.glob("claude-*.log"))[0].read_text()
    assert "result: turns=3 ok" in text


def test_session_log_unknown_event_type_no_summary(_isolated_log_dir: Path) -> None:
    slog = _slog([])
    slog.event({"type": "system_init"})  # not assistant/user/result
    slog.close()

    # No crash.


def test_session_log_assistant_no_text_no_summary(_isolated_log_dir: Path) -> None:
    slog = _slog([])
    # Empty text block — falsy strip.
    slog.event(
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "  "}]},
        }
    )
    slog.close()


def test_session_log_forward_to_runtime_only_logs_first_line(
    _isolated_log_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="physiclaw.agent.claude.session_log"):
        slog = _slog([])
        slog.event(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "first line\nsecond line"},
                    ]
                },
            }
        )
        slog.close()

    runtime_lines = [r for r in caplog.records if "claude:" in r.getMessage()]
    assert any("first line" in r.getMessage() for r in runtime_lines)
    # Second line must not appear in the live runtime log.
    assert not any("second line" in r.getMessage() for r in runtime_lines)


def test_session_log_raw_writes_truncated_text(_isolated_log_dir: Path) -> None:
    slog = _slog([])
    long = "z" * 1000
    slog.raw(long)
    slog.close()

    text = list(_isolated_log_dir.glob("claude-*.log"))[0].read_text()
    # Truncated at 500 chars.
    assert "raw: " in text
    assert "z" * 500 in text
    assert "z" * 501 not in text


def test_session_log_done_parses_sentinel_on_clean_exit(
    _isolated_log_dir: Path,
) -> None:
    slog = _slog([])
    slog.event(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "wrapping up\n>> DONE - all good"},
                ]
            },
        }
    )

    status = slog.done(0)
    slog.close()

    assert status == "DONE"
    text = list(_isolated_log_dir.glob("claude-*.log"))[0].read_text()
    assert "OUTCOME: DONE - all good" in text
    assert "EXIT code=0" in text


def test_session_log_done_undone_on_nonzero_exit(_isolated_log_dir: Path) -> None:
    slog = _slog([])
    slog.event(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": ">> DONE - claimed but crashed"},
                ]
            },
        }
    )

    status = slog.done(1)
    slog.close()

    assert status == "UNDONE"


def test_session_log_done_undone_when_no_text(_isolated_log_dir: Path) -> None:
    slog = _slog([])
    status = slog.done(0)
    slog.close()

    assert status == "UNDONE"
    text = list(_isolated_log_dir.glob("claude-*.log"))[0].read_text()
    assert "(no text)" in text


def test_session_log_done_truncates_recap_to_200_chars(_isolated_log_dir: Path) -> None:
    slog = _slog([])
    slog.event(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "x" * 500},
                ]
            },
        }
    )

    slog.done(0)
    slog.close()

    text = list(_isolated_log_dir.glob("claude-*.log"))[0].read_text()
    # OUTCOME line specifically — text summary line above contains 500 x's.
    outcome_line = next(line for line in text.splitlines() if "OUTCOME" in line)
    assert "OUTCOME: UNDONE" in outcome_line
    # Recap is exactly 200 x's, no more.
    assert "x" * 200 in outcome_line
    assert "x" * 201 not in outcome_line


def test_session_log_rollover_at_midnight(_isolated_log_dir: Path) -> None:
    from freezegun import freeze_time

    with freeze_time("2026-04-28 23:59:59") as frozen:
        slog = _slog([])
        # Cross midnight before the next write.
        frozen.move_to("2026-04-29 00:00:01")
        slog.event({"type": "result", "num_turns": 1, "result": "ok"})
        slog.close()

    # Marker details are pinned by the DailyLogWriter unit tests
    # (tests/common/logger); here just the wiring: a post-midnight write
    # lands in the new day's file.
    files = sorted(_isolated_log_dir.glob("claude-*.log"))
    assert len(files) == 2
    today_text = (_isolated_log_dir / "claude-2026-04-29.log").read_text()
    assert "result: turns=1" in today_text


@pytest.mark.parametrize("value", [None, True, 12, ["unexpected"], {"bad": "shape"}])
def test_regression_malformed_assistant_fields_preserve_valid_text(
    _isolated_log_dir, value
):
    slog = _slog([])
    data = {
        "type": "assistant",
        "message": {
            "id": value,
            "content": [
                {"type": "text", "text": ">> DONE - complete"},
                {"type": "text", "text": value},
                {"type": "thinking", "thinking": value},
                {"type": "tool_use", "name": value},
            ],
        },
    }

    try:
        slog.event(data)
        result = slog.done(0)
    finally:
        slog.close()

    assert result == "DONE"


@pytest.mark.parametrize("source", [None, True, 12, ["unexpected"], "unexpected"])
def test_regression_malformed_image_sources_do_not_break_logging(
    _isolated_log_dir, source
):
    slog = _slog([])
    data = {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "content": [{"type": "image", "source": source}],
                }
            ]
        },
    }

    try:
        result = slog.event(data)
    finally:
        slog.close()

    assert result is None


_METADATA = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(min_value=-(10**30), max_value=10**30)
    | st.floats()
    | st.text(max_size=30),
    lambda values: (
        st.lists(values, max_size=3)
        | st.dictionaries(st.text(max_size=10), values, max_size=3)
    ),
    max_leaves=8,
)


@pytest.mark.parametrize("data", [None, [], ["event"], True, 1, "event"])
def test_non_object_events_are_ignored(_isolated_log_dir, data):
    slog = _slog([])

    try:
        result = slog.event(data)
    finally:
        slog.close()

    assert result is None


def test_summary_write_failure_closes_resources_once(_isolated_log_dir, mocker, caplog):
    slog = _slog([])
    mocker.patch.object(
        session_log, "write_json_atomic", side_effect=OSError("disk full")
    )
    daily_close = mocker.spy(session_log.DailyLogWriter, "close")
    sidecars_close = mocker.spy(session_log.SessionLogSidecars, "close")

    slog.close()
    slog.close()

    daily_close.assert_called_once()
    sidecars_close.assert_called_once()
    assert "summary write failed" in caplog.text


@pytest.mark.parametrize("error", [None, OSError("disk full")])
def test_failed_image_save_does_not_count_an_image(_isolated_log_dir, mocker, error):
    from physiclaw.common import paths

    slog = _slog([])
    save = mocker.patch.object(
        session_log, "save_image", return_value=None, side_effect=error
    )
    event = {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "base64", "data": "AAAA"},
                        }
                    ],
                }
            ]
        },
    }

    try:
        slog.event(event)
    finally:
        slog.close()

    save.assert_called_once()
    summary = json.loads(
        (
            paths.claude_sessions_dir() / "20260101_120000_test00" / "summary.json"
        ).read_text()
    )
    assert summary["images"] == 0


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(value=_METADATA)
def test_external_metadata_keeps_summary_finite_and_preserves_done(
    _isolated_log_dir, value
):
    from physiclaw.common import paths

    # Each example owns a new logger and replaces this sid's summary; no
    # assertions depend on earlier examples or accumulated daily/stats lines.
    slog = _slog([])
    usage = dict.fromkeys(
        [
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        ],
        value,
    )
    event = {
        "type": "result",
        "usage": usage,
        "duration_api_ms": value,
        "total_cost_usd": value,
    }

    try:
        slog.event(
            {
                "type": "assistant",
                "message": {"id": value, "content": ">> DONE - complete"},
            }
        )
        slog.event(event)
        status = slog.done(0)
    finally:
        slog.close()
    summary = json.loads(
        (
            paths.claude_sessions_dir() / "20260101_120000_test00" / "summary.json"
        ).read_text()
    )

    assert status == "DONE" and summary["outcome"]["sentinel"] == "DONE"
    assert math.isfinite(summary["cost_usd"]) and summary["cost_usd"] >= 0
    assert all(
        isinstance(v, int) and v >= 0
        for k, v in summary["usage"].items()
        if k != "cache_hit_pct"
    )
    assert (
        isinstance(summary["provider_time_ms"], int)
        and summary["provider_time_ms"] >= 0
    )
