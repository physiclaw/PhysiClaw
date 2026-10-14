"""Trace persistence through the public session sink."""

import json

import pytest

from physiclaw.agent.trace import Trace
from physiclaw.agent.trace.store import Images
from physiclaw.common import paths


@pytest.mark.parametrize(
    "text, expected",
    [
        ("hello\ud800", r"hello\ud800"),
        ("hello\udfff", r"hello\udfff"),
        ("你好😀", "你好😀"),
    ],
)
def test_regression_trace_note_preserves_unpaired_surrogates(text, expected):
    trace = Trace("note", Images("note"))

    try:
        trace.write(
            {
                "event": "tool_result",
                "turn": 2,
                "name": "note",
                "arguments": {"summary": text},
                "text": "noted",
            }
        )
    finally:
        trace.close()

    notes = (paths.engine_sessions_dir() / "note" / "notes.md").read_text(
        encoding="utf-8"
    )
    assert f"- turn 2 — {expected}\n" in notes


@pytest.mark.parametrize("text", ["hello\ud800", "hello\udfff", "你好😀"])
def test_regression_trace_events_preserve_unpaired_surrogates(text):
    trace = Trace("event", Images("event"))

    try:
        trace.write({"event": "tool_error", "turn": 1, "error": text})
    finally:
        trace.close()

    records = (paths.engine_sessions_dir() / "event" / "events.jsonl").read_text(
        encoding="utf-8"
    )
    assert json.loads(records.splitlines()[-1])["error"] == text


@pytest.mark.parametrize("text", ["hello\ud800", "hello\udfff", "你好😀"])
def test_regression_trace_close_keeps_surrogate_recap_and_completed_outcome(text):
    trace = Trace("finished", Images("finished"))
    trace.write({"event": "done", "sentinel": "DONE", "recap": text})

    trace.close()

    summary = json.loads(
        (paths.engine_sessions_dir() / "finished" / "summary.json").read_text(
            encoding="utf-8"
        )
    )
    assert summary["outcome"] == {"sentinel": "DONE", "recap": text, "crashed": False}
    assert not paths.active_session_marker().exists()


def test_trace_summary_write_failure_still_detaches_session(mocker):
    trace = Trace("failed-summary", Images("failed-summary"))
    mocker.patch(
        "physiclaw.agent.trace.trace.write_json_atomic",
        side_effect=OSError("disk full"),
    )

    trace.close()
    trace.close()

    assert not paths.active_session_marker().exists()
