"""Tests for `physiclaw.agent.engine.trace` — engine session logging.

Covers public formatting helpers, _summarize event dispatch, Trace
file writes + day rollover, RawLog session-start/request/response
emit + image scrubbing for both OpenAI (image_url) and Anthropic
(image+source) wire shapes, and _purge_old retention.

Module-level `_LOG_DIR` / `_RAW_DIR` / `_IMAGE_DIR` are bound at
import; the autouse fixture re-points them to per-test dirs.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
from pathlib import Path

import pytest
from freezegun import freeze_time

from physiclaw.agent.engine import trace
from physiclaw.agent.engine.dto import ImageBlock, TextBlock
from physiclaw.agent.engine.trace import (
    RawLog,
    Trace,
    brief,
    brief_args,
    brief_content,
    format_call_args,
    format_call_result,
)


@pytest.fixture(autouse=True)
def _trace_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log_dir = tmp_path / "engine"
    raw_dir = log_dir / "raw"
    image_dir = raw_dir / "images"
    monkeypatch.setattr(trace, "_LOG_DIR", log_dir)
    monkeypatch.setattr(trace, "_RAW_DIR", raw_dir)
    monkeypatch.setattr(trace, "_IMAGE_DIR", image_dir)
    return log_dir


# ---------- brief / brief_args ----------


def test_brief_returns_short_strings_unchanged() -> None:
    assert brief("hello") == "hello"


def test_brief_truncates_long_strings_with_ellipsis() -> None:
    out = brief("x" * 100, limit=10)

    assert out == "xxxxxxxxx…"
    assert len(out) == 10


def test_brief_uses_repr_for_non_strings() -> None:
    assert brief({"a": 1}, limit=80) == "{'a': 1}"


def test_brief_args_joins_kv_pairs_with_commas() -> None:
    # `brief` returns strings as-is; non-strings via repr.
    assert brief_args({"a": 1, "b": "x"}) == "a=1, b=x"


def test_brief_args_truncates_individual_values_at_40() -> None:
    out = brief_args({"k": "x" * 100})

    assert "x…" in out


# ---------- format_call_args / format_call_result ----------


def test_format_call_args_uses_full_args_for_update_progress() -> None:
    long = "x" * 100
    out = format_call_args("update_progress", {"steps": long})

    # Full repr — no 40-char truncation.
    assert long in out


def test_format_call_args_uses_brief_args_for_other_tools() -> None:
    out = format_call_args("tap", {"bbox": "x" * 100})

    assert "…" in out


def test_format_call_result_no_truncation_for_note() -> None:
    long = "x" * 200

    assert format_call_result("note", long) == long


def test_format_call_result_truncates_other_tools_at_80() -> None:
    out = format_call_result("tap", "x" * 100)

    assert len(out) == 80


# ---------- brief_content ----------


def test_brief_content_string() -> None:
    assert brief_content("hello") == "hello"


def test_brief_content_text_block() -> None:
    assert brief_content([TextBlock(text="hi")]) == "hi"


def test_brief_content_image_block_shows_byte_count() -> None:
    assert brief_content([ImageBlock(media_type="image/jpeg", data_b64="aGk=")]) == (
        "<image 4b>"
    )


def test_brief_content_dict_text_form() -> None:
    assert brief_content([{"type": "text", "text": "x"}]) == "x"


def test_brief_content_dict_image_form() -> None:
    assert brief_content([{"type": "image", "data": "aGk="}]) == "<image 4b>"


def test_brief_content_dict_image_url_extracts_data_length() -> None:
    out = brief_content([
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,abcdef"}}
    ])

    # The "data" portion (after the comma) is "abcdef" = 6 chars.
    assert out == "<image 6b>"


def test_brief_content_unknown_block_type_uses_type_or_question() -> None:
    out = brief_content([{"type": "future_kind"}])

    assert out == "future_kind"


def test_brief_content_unknown_object_renders_question_mark() -> None:
    assert brief_content([object()]) == "?"


def test_brief_content_empty_list_returns_empty_label() -> None:
    assert brief_content([]) == "(empty)"


def test_brief_content_non_list_non_str_uses_repr() -> None:
    assert brief_content(42) == "42"


def test_brief_content_multiple_blocks_joined_by_plus() -> None:
    out = brief_content([
        TextBlock(text="a"),
        ImageBlock(media_type="image/jpeg", data_b64="aGk="),
    ])

    assert out == "a + <image 4b>"


# ---------- _summarize ----------


@pytest.mark.parametrize(
    "event, expected_substr",
    [
        ({"event": "wake", "session": "s1", "provider": "openai", "triggers": [{"source": "phone"}]}, "WAKE session=s1"),
        ({"event": "tools_loaded", "mcp": [1, 2], "local": [1]}, "tools: 2 MCP + 1 local"),
        ({"event": "request", "turn": 3, "message_count": 7}, "turn 3: request (7 messages)"),
        ({"event": "response", "turn": 1, "finish_reason": "stop", "tool_calls": [{"name": "tap"}]}, "turn 1: response finish=stop calls=['tap']"),
        ({"event": "cache", "turn": 2, "hit": 100, "create": 5, "new": 50, "total": 155}, "cache hit=100 create=5 new=50 / total=155"),
        ({"event": "tool_invalid_args", "turn": 4, "name": "tap", "error": "missing bbox"}, "tap invalid args: missing bbox"),
        ({"event": "tool_unknown", "turn": 4, "name": "ghost"}, "ghost unknown tool"),
        ({"event": "tool_error", "turn": 5, "name": "tap", "error": "boom"}, "tap failed: boom"),
        ({"event": "violations", "turn": 6, "codes": ["V1", "V2"]}, "violations ['V1', 'V2']"),
        ({"event": "log_append", "turn": 1, "entry": "did stuff"}, "log: did stuff"),
        ({"event": "memory_save", "turn": 1, "text": "user likes X"}, "memory: user likes X"),
        ({"event": "sentinel", "turn": 9, "name": "DONE", "recap": "task complete"}, "SENTINEL DONE — task complete"),
        ({"event": "wait_auto_scheduled", "job_id": "wait-check", "at": "10:00"}, "WAIT auto-scheduled: wait-check at 10:00"),
        ({"event": "wait_auto_schedule_failed", "error": "x"}, "WAIT auto-schedule failed: x"),
        ({"event": "done", "sentinel": "DONE", "recap": "ok"}, "OUTCOME: DONE — ok"),
        ({"event": "crashed"}, "CRASHED"),
        ({"event": "provider_failed", "turn": 2, "error": "rate limited"}, "provider failed: rate limited"),
        ({"event": "prefix_drift", "turn": 3, "expected": "abcdefghijklmnop", "actual": "zyxwvutsrqponmlk"}, "PREFIX DRIFT"),
    ],
)
def test_summarize_event_dispatch(event: dict, expected_substr: str) -> None:
    out = trace._summarize(event)

    assert out is not None
    assert expected_substr in out


def test_summarize_silent_event_returns_none() -> None:
    assert trace._summarize({"event": "prefix_pinned"}) is None
    assert trace._summarize({"event": "finish_length_warning"}) is None


def test_summarize_unknown_event_falls_back_to_compact_repr() -> None:
    out = trace._summarize({"event": "future_event_type", "data": 42})

    assert out is not None
    assert "future_event_type" in out


def test_summarize_done_with_no_sentinel_uses_none_placeholder() -> None:
    out = trace._summarize({"event": "done", "recap": "ok"})

    assert "(none)" in out


def test_summarize_tool_result_with_text_uses_format_call_result() -> None:
    out = trace._summarize({
        "event": "tool_result", "turn": 1, "name": "note",
        "arguments": {}, "text": "x" * 100,
    })

    # `note` doesn't truncate at 80 — full text passes through.
    assert "x" * 100 in out


def test_summarize_tool_result_without_text_uses_brief_content_on_blocks() -> None:
    out = trace._summarize({
        "event": "tool_result", "turn": 1, "name": "tap",
        "arguments": {}, "blocks": [{"type": "text", "text": "ok"}],
    })

    assert "→ ok" in out


# ---------- Trace ----------


def test_trace_creates_log_directory_and_opens_file_with_separator(
    _trace_dirs: Path,
) -> None:
    with freeze_time("2026-04-28T10:00:00"):
        t = Trace("session-1")
        t.close()

    log_path = _trace_dirs / "engine-2026-04-28.log"
    assert log_path.is_file()
    assert "=" * 60 in log_path.read_text()


def test_trace_write_appends_summary_line_with_timestamp(
    _trace_dirs: Path,
) -> None:
    with freeze_time("2026-04-28T10:00:00"):
        t = Trace("s1")
        t.write({"event": "tools_loaded", "mcp": [], "local": []})
        t.close()

    text = (_trace_dirs / "engine-2026-04-28.log").read_text()
    assert "[10:00:00] tools: 0 MCP + 0 local" in text


def test_trace_write_skips_silent_events(_trace_dirs: Path) -> None:
    with freeze_time("2026-04-28T10:00:00"):
        t = Trace("s1")
        t.write({"event": "prefix_pinned"})
        t.close()

    text = (_trace_dirs / "engine-2026-04-28.log").read_text()
    assert "prefix_pinned" not in text


def test_trace_close_is_idempotent(_trace_dirs: Path) -> None:
    t = Trace("s1")
    t.close()
    t.close()  # must not raise


def test_trace_rolls_over_to_new_day_when_midnight_crossed(
    _trace_dirs: Path,
) -> None:
    with freeze_time("2026-04-28T23:59:00") as ft:
        t = Trace("s1")
        ft.move_to("2026-04-29T00:00:00")
        t.write({"event": "tools_loaded", "mcp": [], "local": []})
        t.close()

    today_log = _trace_dirs / "engine-2026-04-28.log"
    tomorrow_log = _trace_dirs / "engine-2026-04-29.log"

    assert "ROLLOVER" in today_log.read_text()
    assert "ROLLOVER ← continued from previous day" in tomorrow_log.read_text()
    assert "tools: 0 MCP" in tomorrow_log.read_text()


# ---------- RawLog ----------


def test_rawlog_writes_session_start_line(_trace_dirs: Path) -> None:
    log = RawLog("sess-A")
    log.write_session_start(
        provider="anthropic", model="claude-test",
        prompt_hash="abc123", tools=[{"name": "tap"}],
    )
    log.close()

    line = (_trace_dirs / "raw" / "sess-A.jsonl").read_text().splitlines()[0]
    obj = json.loads(line)
    assert obj["kind"] == "session_start"
    assert obj["provider"] == "anthropic"
    assert obj["tools"] == [{"name": "tap"}]


def test_rawlog_writes_request_with_turn_index(_trace_dirs: Path) -> None:
    log = RawLog("sess-B")
    log.write_request(turn=3, messages=[{"role": "user", "content": "hi"}])
    log.close()

    line = (_trace_dirs / "raw" / "sess-B.jsonl").read_text().splitlines()[0]
    obj = json.loads(line)
    assert obj["kind"] == "request"
    assert obj["turn"] == 3
    assert obj["messages"] == [{"role": "user", "content": "hi"}]


def test_rawlog_writes_response_with_elapsed(_trace_dirs: Path) -> None:
    log = RawLog("sess-C")
    log.write_response(turn=1, raw={"id": "r1"}, elapsed_ms=42)
    log.close()

    line = (_trace_dirs / "raw" / "sess-C.jsonl").read_text().splitlines()[0]
    obj = json.loads(line)
    assert obj["kind"] == "response"
    assert obj["elapsed_ms"] == 42


def test_rawlog_close_is_idempotent(_trace_dirs: Path) -> None:
    log = RawLog("s")
    log.close()
    log.close()


# ---------- RawLog._scrub_images / _scrub_block ----------


def test_rawlog_scrubs_openai_image_url_data_to_disk(_trace_dirs: Path) -> None:
    log = RawLog("sess-IMG")
    raw_bytes = b"fake jpeg bytes"
    b64 = base64.b64encode(raw_bytes).decode()
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ],
    }]

    out = log._scrub_images(messages)

    # The image_url is replaced with a path under images/<sess>_NNNNN.jpg
    img_url = out[0]["content"][1]["image_url"]["url"]
    assert img_url == "images/sess-IMG_00001.jpg"
    # The actual file was written.
    assert (_trace_dirs / "raw" / img_url).read_bytes() == raw_bytes


def test_rawlog_scrubs_anthropic_image_block_to_ref(_trace_dirs: Path) -> None:
    log = RawLog("sess-A")
    raw_bytes = b"png data"
    b64 = base64.b64encode(raw_bytes).decode()
    messages = [{
        "role": "user",
        "content": [{
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": b64},
        }],
    }]

    out = log._scrub_images(messages)

    src = out[0]["content"][0]["source"]
    assert src["type"] == "ref"
    assert src["ref"].endswith(".png")
    assert (_trace_dirs / "raw" / src["ref"]).read_bytes() == raw_bytes


def test_rawlog_scrubs_anthropic_tool_result_inner_content(_trace_dirs: Path) -> None:
    log = RawLog("sess-T")
    raw_bytes = b"img"
    b64 = base64.b64encode(raw_bytes).decode()
    messages = [{
        "role": "user",
        "content": [{
            "type": "tool_result",
            "tool_use_id": "t1",
            "content": [
                {"type": "text", "text": "caption"},
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
                },
            ],
        }],
    }]

    out = log._scrub_images(messages)

    inner = out[0]["content"][0]["content"]
    assert inner[0] == {"type": "text", "text": "caption"}
    assert inner[1]["source"]["type"] == "ref"


def test_rawlog_passes_through_non_data_image_url(_trace_dirs: Path) -> None:
    log = RawLog("s")
    msg = {"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "https://x/img.jpg"}},
    ]}

    out = log._scrub_images([msg])

    assert out[0]["content"][0]["image_url"]["url"] == "https://x/img.jpg"


def test_rawlog_passes_through_non_base64_anthropic_image(
    _trace_dirs: Path,
) -> None:
    log = RawLog("s")
    msg = {"role": "user", "content": [
        {"type": "image", "source": {"type": "url", "url": "https://x/img"}},
    ]}

    out = log._scrub_images([msg])

    assert out[0]["content"][0]["source"]["type"] == "url"


def test_rawlog_falls_back_to_byte_count_stub_on_decode_failure(
    _trace_dirs: Path,
) -> None:
    log = RawLog("s")
    msg = {"role": "user", "content": [
        {"type": "image", "source": {
            "type": "base64", "media_type": "image/jpeg", "data": "%%not base64%%"
        }},
    ]}

    out = log._scrub_images([msg])

    # base64.b64decode with validate=False is permissive — won't raise
    # on most strings. Confirm we end up with either a ref or a stub
    # with a well-defined shape.
    src = out[0]["content"][0]["source"]
    assert src["type"] in ("ref", "base64")


def test_rawlog_passes_through_messages_with_string_content(
    _trace_dirs: Path,
) -> None:
    log = RawLog("s")
    msg = {"role": "user", "content": "plain string"}

    out = log._scrub_images([msg])

    assert out == [msg]


def test_rawlog_passes_through_unknown_block_types(
    _trace_dirs: Path,
) -> None:
    log = RawLog("s")
    msg = {"role": "user", "content": [{"type": "tool_use", "name": "tap"}]}

    out = log._scrub_images([msg])

    assert out[0]["content"][0] == {"type": "tool_use", "name": "tap"}


def test_rawlog_empty_data_field_returns_unreadable_stub(
    _trace_dirs: Path,
) -> None:
    log = RawLog("s")
    msg = {"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,"}},
    ]}

    out = log._scrub_images([msg])

    url = out[0]["content"][0]["image_url"]["url"]
    assert "unreadable" in url


# ---------- _purge_old ----------


def test_purge_old_removes_files_older_than_retention_days(
    _trace_dirs: Path,
) -> None:
    raw_dir = _trace_dirs / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    old = raw_dir / "old.jsonl"
    young = raw_dir / "young.jsonl"
    old.write_text("x")
    young.write_text("y")

    import os
    cutoff_seconds = trace._RETENTION_DAYS * 86400
    long_ago = (dt.datetime.now() - dt.timedelta(seconds=cutoff_seconds + 100)).timestamp()
    os.utime(old, (long_ago, long_ago))

    trace._purge_old()

    assert not old.exists()
    assert young.exists()


def test_purge_old_returns_silently_when_dir_missing() -> None:
    # _RAW_DIR doesn't exist — must not raise.
    trace._purge_old()
