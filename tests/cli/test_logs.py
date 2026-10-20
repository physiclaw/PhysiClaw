"""Tests for `physiclaw logs` — session listing and inspection.

Accepted mutation survivors change wording, indentation/Unicode escaping,
missing-field placeholders, or display truncation by one character. Temporary
archive names are private; either nonzero status rejects a session removed
after lookup. A 1024→1025 divisor only changes approximate archive-size output;
the 0→1 ms fallback still displays 0.0s. The missing timestamp fallback remains
empty after slicing, and discovered summaries always have a session ID.
"""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from typer.testing import CliRunner

from physiclaw.cli import app

runner = CliRunner()


def _summary(sid: str, sentinel: str = "DONE", recap: str = "ok") -> dict:
    return {
        "schema": 1,
        "sid": sid,
        "started_at": "x",
        "ended_at": "y",
        "duration_s": 313.0,
        "model_ref": "moonshot/kimi-k3",
        "provider": "moonshot",
        "prompt_hash": "h",
        "triggers": [],
        "outcome": {"sentinel": sentinel, "recap": recap, "crashed": False},
        "turns": 42,
        "provider_calls": 47,
        "provider_time_ms": 1,
        "usage": {
            "input_tokens": 1_200_000,
            "output_tokens": 9_800,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "cache_hit_pct": 87.5,
        },
        "tool_calls": {"note": 42},
        "errors": {},
        "stuck_events": 0,
        "images": 3,
    }


@pytest.fixture
def sessions(physiclaw_home) -> Path:
    from physiclaw.common import paths

    d = paths.sessions_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_session(d: Path, sid: str, **kw) -> Path:
    sd = d / sid
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "summary.json").write_text(json.dumps(_summary(sid, **kw)))
    return sd


def test_logs_lists_recent_sessions(sessions: Path) -> None:
    _write_session(sessions, "20260710_090000")
    _write_session(sessions, "20260710_100000", sentinel="STUCK", recap="lost")

    result = runner.invoke(app, ["logs"])

    assert result.exit_code == 0
    lines = result.output.splitlines()
    # Newest first.
    first = next(x for x in lines if "20260710_100000" in x)
    assert "STUCK" in first and "lost" in first
    assert "1.2M/9.8k" in result.output
    assert "88%" in result.output or "87" in result.output


def test_logs_shows_summaryless_dir_as_unknown(sessions: Path) -> None:
    (sessions / "20260710_110000").mkdir()

    result = runner.invoke(app, ["logs"])

    assert "20260710_110000" in result.output
    assert "?" in result.output


def test_regression_logs_lists_non_object_summary_as_unknown(sessions: Path) -> None:
    directory = _write_session(sessions, "20261014-100000-abc123")
    (directory / "summary.json").write_text('["broken"]', encoding="utf-8")

    result = runner.invoke(app, ["logs"])

    assert result.exit_code == 0, result.output
    assert "20261014-100000-abc123" in result.output
    assert "?" in result.output


def test_regression_logs_rejects_parent_directory(sessions: Path) -> None:
    result = runner.invoke(app, ["logs", "..", "--json"])

    assert result.exit_code == 1
    assert "no session matches" in result.output


def test_logs_empty_state(sessions: Path) -> None:
    result = runner.invoke(app, ["logs"])

    assert result.exit_code == 0
    assert "no sessions yet" in result.output


def test_logs_json_list_round_trips(sessions: Path) -> None:
    _write_session(sessions, "20260710_090000")

    result = runner.invoke(app, ["logs", "--json"])

    data = json.loads(result.output)
    assert data[0]["sid"] == "20260710_090000"


def test_logs_detail_prints_summary_and_narrative(sessions: Path) -> None:
    sd = _write_session(sessions, "20260710_090000")
    events = [
        {
            "t": "2026-07-10T09:00:01.000",
            "event": "wake",
            "session": "s",
            "model_ref": "m/x",
            "triggers": [{"source": "phone"}],
        },
        {
            "t": "2026-07-10T09:00:05.000",
            "event": "done",
            "sentinel": "DONE",
            "recap": "finished",
        },
    ]
    (sd / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")

    result = runner.invoke(app, ["logs", "20260710_090000"])

    assert result.exit_code == 0
    assert '"sid": "20260710_090000"' in result.output
    assert "OUTCOME: DONE — finished" in result.output
    assert "[09:00:05]" in result.output
    assert "model.jsonl" in result.output


def test_logs_detail_json_emits_summary(sessions: Path) -> None:
    _write_session(sessions, "20260710_090000")

    result = runner.invoke(app, ["logs", "20260710_090000", "--json"])

    assert json.loads(result.output)["turns"] == 42


def test_logs_detail_unknown_sid_exits_nonzero(sessions: Path) -> None:
    result = runner.invoke(app, ["logs", "nope"])

    assert result.exit_code == 1
    assert "no session matches" in result.output


def test_logs_resolves_session_by_hex_suffix(sessions: Path) -> None:
    _write_session(sessions, "20260710_090000_ab12cd")

    result = runner.invoke(app, ["logs", "ab12cd"])

    assert result.exit_code == 0
    assert '"sid": "20260710_090000_ab12cd"' in result.output


def test_logs_ambiguous_suffix_lists_candidates(sessions: Path) -> None:
    _write_session(sessions, "20260710_090000_aaa000")
    _write_session(sessions, "20260710_100000_bbb000")

    result = runner.invoke(app, ["logs", "000"])

    assert result.exit_code == 1
    assert "ambiguous" in result.output
    assert "20260710_090000_aaa000" in result.output
    assert "20260710_100000_bbb000" in result.output


def test_logs_unmatched_suffix_reports_missing(sessions: Path) -> None:
    _write_session(sessions, "20260710_090000_ab12cd")

    result = runner.invoke(app, ["logs", "deadbeef"])

    assert result.exit_code == 1
    assert "no session matches" in result.output


# ---------- --save ----------


def _populate(sd: Path) -> None:
    (sd / "events.jsonl").write_text('{"event": "env"}\n')
    (sd / "model.jsonl").write_text('{"kind": "session_start"}\n')
    img = sd / "images"
    img.mkdir(exist_ok=True)
    (img / "090000_000_t0.jpg").write_bytes(b"jpeg")


def test_save_packs_session_zip_with_privacy_notice(
    sessions: Path, tmp_path, monkeypatch
) -> None:
    import zipfile

    sd = _write_session(
        sessions,
        "20260710_090000_ab12cd",
        sentinel="STUCK",
        recap="lost on home screen",
    )
    _populate(sd)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["logs", "ab12cd", "--save"])

    assert result.exit_code == 0
    out = tmp_path / "physiclaw-session-20260710_090000_ab12cd.zip"
    assert out.is_file()
    names = set(zipfile.ZipFile(out).namelist())
    assert "20260710_090000_ab12cd/summary.json" in names
    assert "20260710_090000_ab12cd/events.jsonl" in names
    assert "20260710_090000_ab12cd/model.jsonl" in names
    assert "20260710_090000_ab12cd/images/090000_000_t0.jpg" in names
    # The zip reviews itself: the viewer page rides along, beside its frames.
    assert "20260710_090000_ab12cd/session.html" in names
    # Privacy notice; no issue-filing prompts.
    assert "PRIVATE" in result.output
    assert "review before sharing" in result.output
    assert "issues" not in result.output


def test_save_without_sid_errors(sessions: Path) -> None:
    result = runner.invoke(app, ["logs", "--save"])

    assert result.exit_code == 1
    assert "needs a session" in result.output


def test_save_unknown_sid_errors(sessions: Path, tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["logs", "nope", "--save"])

    assert result.exit_code == 1
    assert "no session matches" in result.output


def test_detail_view_hints_at_review_and_save(sessions: Path) -> None:
    _write_session(sessions, "20260710_090000_ab12cd")

    result = runner.invoke(app, ["logs", "ab12cd"])

    assert "physiclaw studio --review --session ab12cd" in result.output
    assert "physiclaw logs ab12cd --save" in result.output


def test_save_accepts_destination_dir(sessions: Path, tmp_path, monkeypatch) -> None:
    sd = _write_session(sessions, "20260710_090000_ab12cd")
    _populate(sd)
    dest = tmp_path / "backups"
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["logs", "ab12cd", "--save", str(dest)])

    assert result.exit_code == 0
    assert (dest / "physiclaw-session-20260710_090000_ab12cd.zip").is_file()


def test_save_accepts_explicit_zip_path(sessions: Path, tmp_path, monkeypatch) -> None:
    sd = _write_session(sessions, "20260710_090000_ab12cd")
    _populate(sd)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["logs", "ab12cd", "--save", str(tmp_path / "bad.zip")])

    assert result.exit_code == 0
    assert (tmp_path / "bad.zip").is_file()


def test_destination_without_save_is_refused(sessions: Path) -> None:
    _write_session(sessions, "20260710_090000_ab12cd")

    result = runner.invoke(app, ["logs", "ab12cd", "out.zip"])

    assert result.exit_code == 1
    assert "needs --save" in result.output


def test_save_zip_embeds_format_readme(sessions: Path, tmp_path, monkeypatch) -> None:
    import zipfile

    sd = _write_session(sessions, "20260710_090000_ab12cd")
    _populate(sd)
    monkeypatch.chdir(tmp_path)

    runner.invoke(app, ["logs", "ab12cd", "--save"])

    z = zipfile.ZipFile(tmp_path / "physiclaw-session-20260710_090000_ab12cd.zip")
    readme = z.read("20260710_090000_ab12cd/README.md").decode("utf-8")
    assert "events.jsonl" in readme
    # arcnames are forward-slashed on every platform
    assert all("\\\\" not in n for n in z.namelist())


def test_logs_list_orders_by_mtime_across_sid_formats(sessions: Path) -> None:
    # '-' < '_' lexicographically, so a name sort would list a morning
    # legacy `_` session above a newer `-` session from the same day —
    # and [:n] could drop the genuinely newest sessions entirely.
    import os
    import time as time_mod

    from physiclaw.agent.trace.store import recent_sessions

    old = _write_session(sessions, "20260717_090000_aaaaaa")
    new = _write_session(sessions, "20260717-100000-bbbbbb")
    now = time_mod.time()
    os.utime(old, (now - 100, now - 100))
    os.utime(new, (now, now))

    sids = [s["sid"] for s in recent_sessions(sessions, 10)]

    assert sids == ["20260717-100000-bbbbbb", "20260717_090000_aaaaaa"]


def _usage_event(turn, call, model, inp, new, read, write, out) -> str:
    return json.dumps(
        {
            "t": "2026-07-10T09:00:00.000",
            "event": "usage",
            "turn": turn,
            "call": call,
            "model": model,
            "elapsed_ms": 1500,
            "input": inp,
            "input_new": new,
            "cache_read": read,
            "cache_write": write,
            "output": out,
        }
    )


def test_logs_usage_tabulates_every_model_call_and_totals_per_model(
    sessions: Path,
) -> None:
    sd = _write_session(sessions, "20260710_090000")
    (sd / "events.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"t": "x", "event": "env"}),
                _usage_event(
                    0, "turn", "moonshot/kimi-k3", 19000, 2000, 16600, 400, 300
                ),
                _usage_event(0, "micro", "moonshot/kimi-k2.6", 1500, 700, 800, 0, 30),
                _usage_event(1, "turn", "moonshot/kimi-k3", 21000, 1000, 20000, 0, 250),
            ]
        )
        + "\n"
    )

    result = runner.invoke(app, ["logs", "20260710_090000", "--usage"])

    assert result.exit_code == 0, result.output
    assert "moonshot/kimi-k2.6" in result.output and "micro" in result.output
    assert "1.5s" in result.output
    # Per-model totals are exact counts, not rounded — the bill's inputs.
    assert "input_new" in result.output and "3,000" in result.output
    assert "36,600" in result.output  # kimi-k3 cache_read across both turns

    rows = json.loads(
        runner.invoke(app, ["logs", "20260710_090000", "--usage", "--json"]).output
    )
    assert [r["turn"] for r in rows] == [0, 0, 1]
    assert rows[1]["model"] == "moonshot/kimi-k2.6" and rows[1]["output"] == 30
    assert "event" not in rows[0]


def test_logs_usage_without_events_says_so(sessions: Path) -> None:
    _write_session(sessions, "20260710_090000")

    result = runner.invoke(app, ["logs", "20260710_090000", "--usage"])

    assert result.exit_code == 0 and "no usage events" in result.output


def test_logs_usage_includes_native_decisions_and_exposes_unknown_counts(sessions):
    sd = _write_session(sessions, "decision-abc123")
    events = [
        json.loads(_usage_event(0, "micro", "vendor/model", 100, 60, 40, 0, 10)),
        {"event": "micro_call", "model": "vendor/model", "input": 5000},
        {
            "event": "decision_call",
            "request_id": "answered-request",
            "model": "vendor/model",
            "turn": 0,
            "mode": "shadow",
            "status": "answered",
            "elapsed_ms": 420,
            "usage": {"input_tokens": 250, "output_tokens": 0},
        },
        {
            "event": "decision_call",
            "request_id": "failed-request",
            "model": "vendor/model",
            "turn": 1,
            "mode": "shadow",
            "status": "error",
            "elapsed_ms": 2000,
            "error": "TimeoutError",
            "usage": {"input_tokens": None, "output_tokens": None},
        },
    ]
    (sd / "events.jsonl").write_text("\n".join(map(json.dumps, events)))

    result = runner.invoke(app, ["logs", "abc123", "--usage"])

    assert result.exit_code == 0, result.output
    assert "chat vendor/model  (1 calls, 0 failed)" in result.output
    assert "decision vendor/model  (2 calls, 1 failed)" in result.output
    assert "250 known  (1 missing)" in result.output
    assert "?" in result.output and "TimeoutError" in result.output
    rows = json.loads(
        runner.invoke(app, ["logs", "abc123", "--usage", "--json"]).output
    )
    assert len(rows) == 3
    assert [r["api_kind"] for r in rows] == ["chat", "decision", "decision"]
    assert rows[1]["input"] == 250 and rows[1]["output"] == 0
    assert rows[2]["input"] is None and rows[2]["output"] is None


def test_usage_with_rates_reports_partial_cost_as_unknown(sessions, capsys):
    from physiclaw.cli.logs import _show_usage

    sd = _write_session(sessions, "cost-abc123")
    events = [
        {
            "event": "decision_call",
            "request_id": "known-request",
            "model": "vendor/model",
            "usage": {"input_tokens": 100, "output_tokens": 0},
        },
        {
            "event": "decision_call",
            "request_id": "unknown-request",
            "model": "vendor/model",
            "usage": None,
        },
    ]
    (sd / "events.jsonl").write_text("\n".join(map(json.dumps, events)))
    rates = {"decision": {"vendor/model": {"input": 0.042}}}

    _show_usage(sd, as_json=False, rates=rates)
    assert "total unknown (1 incomplete calls)" in capsys.readouterr().out
    _show_usage(sd, as_json=True, rates=rates)
    data = json.loads(capsys.readouterr().out)
    assert len(data["rows"]) == 2
    assert data["cost"]["known_usd"] == pytest.approx(0.0000042)
    assert data["cost"]["total_usd"] is None


@pytest.fixture
def surrogate_session(sessions):
    directory = _write_session(sessions, "unicode-abc123", recap="你好\ud800")
    events = [
        {"event": "done", "sentinel": "DONE", "recap": "你好\ud800"},
        {
            "event": "usage",
            "call": "turn",
            "model": "test/model",
            "error": "你好\ud800",
        },
    ]
    (directory / "events.jsonl").write_text(
        "\n".join(map(json.dumps, events)), encoding="utf-8"
    )
    return directory


@pytest.mark.parametrize("arguments", [[], ["abc123"], ["abc123", "--usage"]])
def test_regression_logs_text_modes_escape_unpaired_surrogates(
    surrogate_session, arguments
):
    result = runner.invoke(app, ["logs", *arguments])

    assert result.exit_code == 0, result.output
    assert "你好\\ud800" in result.output


@pytest.mark.parametrize(
    "arguments, extract",
    [
        ([], lambda data: data[0]["outcome"]["recap"]),
        (["abc123"], lambda data: data["outcome"]["recap"]),
        (["abc123", "--usage"], lambda data: data[0]["error"]),
    ],
)
def test_regression_logs_json_modes_preserve_unpaired_surrogates(
    surrogate_session, arguments, extract
):
    result = runner.invoke(app, ["logs", *arguments, "--json"])

    assert result.exit_code == 0, result.output
    assert extract(json.loads(result.output)) == "你好\ud800"


@pytest.mark.parametrize(
    "broken", [b"[]", b"null", b'"text"', b"17", b"{torn", b"\xff"]
)
@pytest.mark.parametrize("mode", [[], ["--usage"], ["--usage", "--json"]])
def test_regression_logs_damaged_line_keeps_healthy_neighbors(sessions, broken, mode):
    directory = _write_session(sessions, "damaged-abc123")
    healthy = _usage_event(2, "turn", "test/model", 123, 100, 23, 0, 11).encode()
    (directory / "events.jsonl").write_bytes(
        healthy + b"\n" + broken + b"\n" + healthy + b"\n"
    )

    result = runner.invoke(app, ["logs", "abc123", *mode])

    assert result.exit_code == 0, result.output
    assert result.output.count("test/model") >= 2


@pytest.mark.parametrize(
    "event",
    [
        {"event": "usage", "input": "not-a-number"},
        {"event": "wake", "triggers": [None]},
        {"event": "tool_result", "arguments": [1]},
        {"event": []},
    ],
)
def test_regression_logs_bad_event_format_keeps_later_events(sessions, event):
    directory = _write_session(sessions, "format-abc123")
    (directory / "events.jsonl").write_text(
        json.dumps(event)
        + '\n{"event":"done","sentinel":"DONE","recap":"healthy tail"}\n',
        encoding="utf-8",
    )

    result = runner.invoke(app, ["logs", "abc123"])

    assert result.exit_code == 0, result.output
    assert "unformatted event" in result.output
    assert "healthy tail" in result.output


@pytest.mark.parametrize("bad", ["not-a-number", [1], {"a": 2}, float("inf")])
def test_regression_logs_bad_usage_does_not_abort_or_partially_bill(sessions, bad):
    directory = _write_session(sessions, "usage-abc123")
    damaged = {"event": "usage", "model": "test/model", "input": 999, "output": bad}
    healthy = _usage_event(1, "turn", "test/model", 123, 100, 23, 0, 11)
    (directory / "events.jsonl").write_text(
        json.dumps(damaged) + "\n" + healthy + "\n", encoding="utf-8"
    )

    result = runner.invoke(app, ["logs", "abc123", "--usage"])

    assert result.exit_code == 0, result.output
    assert "excluded from totals" in result.output
    assert "test/model  (1 calls, 0 failed)" in result.output
    assert "1,122" not in result.output
    assert "123" in result.output


@pytest.mark.parametrize(
    "patch",
    [
        {"outcome": [1]},
        {"outcome": {"sentinel": [1]}},
        {"usage": {"input_tokens": "bad"}},
        {"duration_s": "bad"},
        {"turns": [1]},
    ],
)
def test_regression_logs_bad_summary_does_not_hide_other_sessions(sessions, patch):
    bad = _write_session(sessions, "damaged-abc123")
    (bad / "summary.json").write_text(
        json.dumps({**_summary(bad.name), **patch}), encoding="utf-8"
    )
    _write_session(sessions, "healthy-def456")

    result = runner.invoke(app, ["logs"])

    assert result.exit_code == 0, result.output
    assert "damaged-abc123" in result.output and "unformatted summary" in result.output
    assert "healthy-def456" in result.output and "DONE" in result.output


@pytest.mark.parametrize("n", ["0", "-1"])
@pytest.mark.parametrize("mode", [[], ["abc123"]])
def test_logs_rejects_nonpositive_limit(sessions, n, mode):
    _write_session(sessions, "limit-abc123")

    result = runner.invoke(app, ["logs", *mode, "-n", n])

    assert result.exit_code == 2
    assert "Invalid value" in result.output


def test_regression_save_inside_session_excludes_previous_archive(sessions):
    directory = _write_session(sessions, "save-abc123")
    _populate(directory)
    out = directory / "backup.zip"
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("old.txt", "previous backup")

    result = runner.invoke(app, ["logs", "abc123", "--save", str(out)])

    assert result.exit_code == 0, result.output
    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())
        assert z.testzip() is None
    assert "save-abc123/backup.zip" not in names
    assert "save-abc123/events.jsonl" in names
    assert not any(".physiclaw-session-" in name for name in names)
    assert not list(directory.glob(".physiclaw-session-*"))


def test_regression_save_refuses_to_replace_session_source(sessions):
    directory = _write_session(sessions, "save-abc123")
    _populate(directory)
    source = directory / "events.jsonl"
    original = source.read_bytes()

    result = runner.invoke(app, ["logs", "abc123", "--save", str(source)])

    assert result.exit_code == 1
    assert "directory or a .zip file" in result.output
    assert source.read_bytes() == original


@pytest.mark.parametrize("operation", ["read", "replace"])
def test_regression_save_failure_preserves_previous_archive(
    sessions, tmp_path, mocker, operation
):
    directory = _write_session(sessions, "save-abc123")
    _populate(directory)
    out = tmp_path / "backup.zip"
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("old.txt", "previous backup")
    original = out.read_bytes()
    target = {"read": "zipfile.ZipFile.write", "replace": "pathlib.Path.replace"}[
        operation
    ]
    mocker.patch(target, side_effect=OSError("simulated export failure"))

    result = runner.invoke(app, ["logs", "abc123", "--save", str(out)])

    assert result.exit_code == 1
    assert "could not save session" in result.output
    assert out.read_bytes() == original
    assert not list(tmp_path.glob(".physiclaw-session-*"))


@pytest.mark.parametrize(
    "flags", [["--save", "--usage"], ["--save", "--json"], ["--usage", "out.zip"]]
)
def test_logs_rejects_conflicting_modes(sessions, flags):
    _write_session(sessions, "mode-abc123")

    result = runner.invoke(app, ["logs", "abc123", *flags])

    assert result.exit_code == 1
    assert "--save" in result.output


@pytest.mark.parametrize("mode", [[], ["abc123"], ["abc123", "--usage"]])
def test_logs_json_rejects_nonfinite_data_without_partial_stdout(sessions, mode):
    directory = _write_session(sessions, "finite-abc123")
    (directory / "summary.json").write_text(
        '{"duration_s": Infinity}', encoding="utf-8"
    )
    (directory / "events.jsonl").write_text(
        '{"event":"usage","input":NaN}\n', encoding="utf-8"
    )

    result = runner.invoke(app, ["logs", *mode, "--json"])

    if "--usage" in mode:
        # Offline accounting excludes nonfinite lines at the strict reader;
        # rows remain valid JSON, and priced output marks the trace incomplete.
        assert result.exit_code == 0
        assert json.loads(result.output) == []
        return
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "cannot render log data as JSON" in result.stderr


def test_save_includes_one_current_format_readme(sessions, tmp_path):
    directory = _write_session(sessions, "readme-abc123")
    (directory / "README.md").write_text("old format", encoding="utf-8")
    out = tmp_path / "session.zip"

    result = runner.invoke(app, ["logs", "abc123", "--save", str(out)])

    assert result.exit_code == 0, result.output
    with zipfile.ZipFile(out) as z:
        assert z.namelist().count("readme-abc123/README.md") == 1
        assert "events.jsonl" in z.read("readme-abc123/README.md").decode("utf-8")


@given(
    st.lists(
        st.fixed_dictionaries(
            {
                "turn": st.one_of(st.none(), st.integers(min_value=0, max_value=50)),
                "model": st.sampled_from(["test/first", "test/second"]),
                "input": st.integers(min_value=0, max_value=1_000_000),
                "output": st.integers(min_value=0, max_value=1_000_000),
            }
        ),
        max_size=12,
    )
)
@settings(max_examples=25, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_usage_json_preserves_generated_rows_around_damaged_lines(sessions, rows):
    directory = _write_session(sessions, "property-abc123")
    records = [
        json.dumps({"event": "usage", "t": "stamp", **row}).encode() for row in rows
    ]
    (directory / "events.jsonl").write_bytes(
        b"\xff\n[]\n" + b"\nnull\n".join(records) + b"\n"
    )

    result = runner.invoke(app, ["logs", "abc123", "--usage", "--json"])

    assert result.exit_code == 0, result.output
    actual = json.loads(result.output)
    for row in actual:
        assert row.pop("api_kind") == "chat"
        assert row.pop("usage_missing") == (row["input"] == row["output"] == 0)
    assert actual == rows


def test_logs_crashed_session_is_distinct_from_terminal_sentinel(sessions):
    directory = _write_session(sessions, "crashed-abc123")
    summary = _summary(directory.name)
    summary["outcome"]["crashed"] = True
    (directory / "summary.json").write_text(json.dumps(summary), encoding="utf-8")

    result = runner.invoke(app, ["logs"])

    assert result.exit_code == 0
    assert "CRASH" in result.output and "DONE" not in result.output


@pytest.mark.parametrize("mode", [[], ["--json"]])
def test_logs_detail_of_unfinished_session_keeps_session_id(sessions, mode):
    (sessions / "unfinished-abc123").mkdir()

    result = runner.invoke(app, ["logs", "abc123", *mode])

    assert result.exit_code == 0
    assert "summary" in result.output
    assert "abc123" in result.output


def test_logs_unreadable_event_stream_keeps_summary(sessions):
    directory = _write_session(sessions, "unreadable-abc123")
    (directory / "events.jsonl").mkdir()

    result = runner.invoke(app, ["logs", "abc123"])

    assert result.exit_code == 0
    assert "unreadable-abc123" in result.output
    assert "no events.jsonl" in result.output


def test_logs_narrative_limit_uses_only_the_tail_and_keeps_silent_events_silent(
    sessions,
):
    directory = _write_session(sessions, "tail-abc123")
    events = [
        {"event": "log_append", "entry": "old story"},
        {"event": "prefix_pinned", "hash": "secret hash"},
        {"event": "log_append", "entry": "new story"},
    ]
    (directory / "events.jsonl").write_text(
        "\n".join(map(json.dumps, events)), encoding="utf-8"
    )

    result = runner.invoke(app, ["logs", "abc123", "-n", "2"])

    assert result.exit_code == 0
    assert "Last 2 of 3 events" in result.output
    assert "new story" in result.output
    assert "old story" not in result.output and "secret hash" not in result.output


def test_logs_default_list_limit_is_twenty_sessions(sessions):
    directories = [
        _write_session(sessions, f"session-{index:06}") for index in range(21)
    ]

    result = runner.invoke(app, ["logs", "--json"])

    assert result.exit_code == 0
    assert len(json.loads(result.output)) == 20
    assert len(directories) == 21


def test_save_creates_missing_destination_ancestors(sessions, tmp_path):
    _write_session(sessions, "nested-abc123")
    out = tmp_path / "first" / "second" / "session.zip"

    result = runner.invoke(app, ["logs", "abc123", "--save", str(out)])

    assert result.exit_code == 0, result.output
    assert zipfile.is_zipfile(out)


def test_save_reports_screenshot_count_and_rounded_archive_size(sessions, tmp_path):
    directory = _write_session(sessions, "metrics-abc123")
    _populate(directory)
    out = tmp_path / "session.zip"

    result = runner.invoke(app, ["logs", "abc123", "--save", str(out)])

    assert result.exit_code == 0, result.output
    assert "(1 screenshots," in result.output
    match = re.search(r"(\d+) KB\)", result.output)
    assert match is not None
    assert abs(int(match[1]) * 1024 - out.stat().st_size) <= 512


def test_logs_detail_links_actual_artifact_paths(sessions):
    directory = _write_session(sessions, "paths-abc123")
    _populate(directory)

    result = runner.invoke(app, ["logs", "abc123"])

    assert result.exit_code == 0
    assert str(directory / "model.jsonl") in result.output
    assert str(directory / "images") in result.output


@pytest.mark.parametrize("turn, displayed", [(None, "-"), (0, "0"), (3, "3")])
def test_usage_table_keeps_turn_zero_unknown_buckets_timing_and_failure(
    sessions, turn, displayed
):
    directory = _write_session(sessions, "columns-abc123")
    event = {
        "event": "usage",
        "turn": turn,
        "call": "turn",
        "model": "test/model",
        "elapsed_ms": 1350,
        "error": "TimeoutError",
    }
    (directory / "events.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")

    result = runner.invoke(app, ["logs", "abc123", "--usage"])

    assert result.exit_code == 0, result.output
    row = next(
        line for line in result.output.splitlines() if "error=TimeoutError" in line
    )
    assert row.split() == [
        displayed,
        "chat",
        "turn",
        "test/model",
        "1.4s",
        "?",
        "?",
        "?",
        "?",
        "?",
        "?",
        "error=TimeoutError",
    ]
    assert "test/model  (1 calls, 1 failed)" in result.output
