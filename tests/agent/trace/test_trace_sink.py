"""Trace persistence through the public session sink.

Accepted mutation survivors change diagnostic wording/traceback verbosity,
missing-field placeholders, or the image turn fallback (engine results supply
turns). Equivalent variants change falsy initial state, unevaluated annotations,
same-turn assignment, or assertions over the current warning roster. Images
already creates the session directory; the constructor always observes env.
"""

import base64
import builtins
import json
import logging
from collections import Counter, defaultdict
from hashlib import sha256
from pathlib import Path

import pytest
from freezegun import freeze_time
from hypothesis import given
from hypothesis import strategies as st

from physiclaw.agent.trace import Trace, viewer
from physiclaw.agent.trace.store import Images
from physiclaw.agent.trace.trace import fold_usage
from physiclaw.common import paths
from physiclaw.contract.dto import USAGE_BUCKETS


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

    notes = (paths.sessions_dir() / "note" / "notes.md").read_text(encoding="utf-8")
    assert f"- turn 2 — {expected}\n" in notes


@pytest.mark.parametrize("text", ["hello\ud800", "hello\udfff", "你好😀"])
def test_regression_trace_events_preserve_unpaired_surrogates(text):
    trace = Trace("event", Images("event"))

    try:
        trace.write({"event": "tool_error", "turn": 1, "error": text})
    finally:
        trace.close()

    records = (paths.sessions_dir() / "event" / "events.jsonl").read_text(
        encoding="utf-8"
    )
    assert json.loads(records.splitlines()[-1])["error"] == text


def test_event_image_preserves_exact_observation_bytes(observation_blocks):
    from physiclaw.contract.observation import read_observation

    blocks = observation_blocks()
    observation = read_observation(blocks)
    assert observation is not None
    event = {
        "event": "input_evidence",
        "turn": 2,
        "image": {
            "media_type": observation.frame.media_type,
            "data_b64": blocks[0]["data"],
        },
    }
    trace = Trace("input-image", Images("input-image"))
    try:
        assert trace.write(event) is True
    finally:
        trace.close()

    directory = paths.sessions_dir() / "input-image"
    text = (directory / "events.jsonl").read_text(encoding="utf-8")
    recorded = json.loads(text.splitlines()[-1])
    image = recorded["image"]
    assert image["sha256"] == observation.frame.sha256
    assert (directory / image["path"]).read_bytes() == observation.frame.data
    assert set(image) == {"path", "sha256"}
    assert "data_b64" not in text
    assert event["image"]["data_b64"] == blocks[0]["data"]


@pytest.mark.parametrize("failure", ["invalid_base64", "save_failed"])
def test_missing_event_image_stays_explicit_without_inline_bytes(mocker, failure):
    data = base64.b64encode(b"exact frame").decode()
    images = Images("missing-image")
    if failure == "invalid_base64":
        data = "not valid base64!"
    else:
        mocker.patch.object(images, "put", return_value="")
    trace = Trace("missing-image", images)
    try:
        assert (
            trace.write(
                {
                    "event": "input_evidence",
                    "turn": 2,
                    "image": {"media_type": "image/jpeg", "data_b64": data},
                }
            )
            is False
        )
    finally:
        trace.close()

    text = (paths.sessions_dir() / "missing-image" / "events.jsonl").read_text(
        encoding="utf-8"
    )
    record = json.loads(text.splitlines()[-1])
    assert record["image"] == {
        "path": None,
        "sha256": (
            None if failure == "invalid_base64" else sha256(b"exact frame").hexdigest()
        ),
    }
    assert data not in text
    assert "data_b64" not in text


@pytest.mark.parametrize("text", ["hello\ud800", "hello\udfff", "你好😀"])
def test_regression_trace_close_keeps_surrogate_recap_and_completed_outcome(text):
    trace = Trace("finished", Images("finished"))
    trace.write({"event": "done", "sentinel": "DONE", "recap": text})

    trace.close()

    summary = json.loads(
        (paths.sessions_dir() / "finished" / "summary.json").read_text(encoding="utf-8")
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


def test_trace_saves_its_artifact_close_failures_before_detaching(mocker, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    trace = Trace("close-debug", Images("close-debug"))
    notes = trace._notes
    trace._notes = mocker.MagicMock(wraps=notes, closed=False)
    trace._notes.close.side_effect = OSError("notes close failure detail")
    try:
        trace.close()
    finally:
        notes.close()

    text = (paths.sessions_dir() / "close-debug" / "runtime.log").read_text(
        encoding="utf-8"
    )
    assert "OSError: notes close failure detail" in text
    assert not paths.active_session_marker().exists()


@pytest.fixture
def blocked_summary():
    trace = Trace("blocked-summary", Images("blocked-summary"))
    trace.write({"event": "done", "sentinel": "DONE", "recap": "finished"})
    (paths.sessions_dir() / "blocked-summary" / "summary.json").mkdir()
    try:
        yield trace
    finally:
        trace.close()


def test_regression_summary_write_failure_keeps_durable_stats(blocked_summary):
    blocked_summary.close()
    blocked_summary.close()

    summary = json.loads(
        (paths.LOG_DIR / "session-stats.jsonl").read_text(encoding="utf-8")
    )
    assert summary["sid"] == "blocked-summary"
    assert summary["outcome"] == {
        "sentinel": "DONE",
        "recap": "finished",
        "crashed": False,
    }


def test_regression_summary_write_failure_keeps_runtime_outcome(
    blocked_summary, caplog
):
    caplog.set_level(logging.INFO)
    blocked_summary.close()

    runtime = (paths.sessions_dir() / "blocked-summary" / "runtime.log").read_text(
        encoding="utf-8"
    )
    assert "END session=blocked-summary outcome=DONE" in runtime


def test_regression_burned_macro_block_is_visible_as_viewer_warning():
    trace = Trace("blocked-macro", Images("blocked-macro"))

    try:
        trace.write(
            {
                "event": "tool_blocked_burned_macro",
                "turn": 0,
                "name": "run_macro",
                "arguments": {"name": "demo/flow"},
            }
        )
    finally:
        trace.close()

    model = viewer.load(paths.sessions_dir() / "blocked-macro")
    (blocked,) = [
        row
        for group in model["groups"]
        for row in group["records"]
        if row["rec"].get("event") == "tool_blocked_burned_macro"
    ]
    assert blocked.get("warn") is True
    assert "macro" in blocked["words"] and "failed" in blocked["words"]


def _fold(events):
    totals = defaultdict(Counter)
    for event in events:
        fold_usage(totals, event)
    return totals


@given(
    st.lists(
        st.fixed_dictionaries(
            {
                "model": st.sampled_from(["test/first", "test/second", "", None]),
                "error": st.sampled_from([None, "TimeoutError"]),
                **{
                    key: st.integers(min_value=0, max_value=1_000_000)
                    for key in USAGE_BUCKETS
                },
            }
        ),
        max_size=15,
    )
)
def test_usage_fold_preserves_every_models_calls_and_buckets(events):
    totals = _fold(events)

    assert totals == _fold(reversed(events))
    assert totals == {
        model: Counter(
            {
                "calls": sum((event["model"] or "?") == model for event in events),
                "failed": sum(
                    bool(event["error"])
                    for event in events
                    if (event["model"] or "?") == model
                ),
                **{
                    key: sum(
                        event[key]
                        for event in events
                        if (event["model"] or "?") == model
                    )
                    for key in USAGE_BUCKETS
                },
            }
        )
        for model in {event["model"] or "?" for event in events}
    }


@pytest.mark.parametrize(
    "event, error_key, stuck_count",
    [
        ("tool_blocked_no_plan", "blocked_plan", 0),
        ("tool_blocked_layout", "blocked_layout", 0),
        ("tool_blocked_stuck", "blocked_stuck", 1),
        ("tool_invalid_args", "invalid_args", 0),
        ("tool_unknown", "unknown_tool", 0),
        ("tool_error", "tool_errors", 0),
        ("provider_failed", "provider_failures", 0),
        ("bad_turn_shape", "correctives", 0),
        ("checkpoint_corrective", "correctives", 0),
        ("stuck_reflection", "correctives", 0),
        ("pitfall_checkpoint", "correctives", 0),
        ("memory_cue_checkpoint", "correctives", 0),
    ],
)
def test_summary_classifies_rejected_and_failed_events(event, error_key, stuck_count):
    trace = Trace("errors", Images("errors"))

    try:
        trace.write({"event": event, "turn": 2, "name": "tap"})
        trace.write({"event": event, "turn": 2, "name": "tap"})
    finally:
        trace.close()

    summary = json.loads(
        (paths.sessions_dir() / "errors" / "summary.json").read_text(encoding="utf-8")
    )
    assert summary["errors"][error_key] == 2
    assert sum(summary["errors"].values()) == 2
    assert summary["stuck_events"] == 2 * stuck_count
    assert summary["turns"] == 3


@pytest.mark.parametrize(
    "changed, bucket", [(True, "changed"), (False, "unchanged"), (None, "no_verdict")]
)
def test_summary_keeps_camera_verdicts_in_separate_buckets(changed, bucket):
    trace = Trace("verdicts", Images("verdicts"))

    try:
        trace.write(
            {
                "event": "tool_result",
                "turn": 0,
                "name": "tap",
                "changed": changed,
                "elapsed_ms": 17,
            }
        )
        trace.write(
            {
                "event": "tool_result",
                "turn": 0,
                "name": "tap",
                "changed": changed,
                "elapsed_ms": 17,
            }
        )
        trace.write({"event": "tool_error", "turn": 1, "name": "tap", "elapsed_ms": 23})
    finally:
        trace.close()

    summary = json.loads(
        (paths.sessions_dir() / "verdicts" / "summary.json").read_text(encoding="utf-8")
    )
    assert summary["verdicts"] == {bucket: 2}
    assert summary["tool_time_ms"] == 57
    assert summary["tool_calls"] == {"tap": 2}


class _FaultyFile:
    def __init__(self, file, fault):
        self.file = file
        self.fault = fault

    def __getattr__(self, name):
        return getattr(self.file, name)

    def _call(self, operation, *args):
        if (
            self.fault.get("armed")
            and self.fault["operation"] == operation
            and self.file.name.endswith(self.fault["name"])
        ):
            self.fault["armed"] = False
            if operation == "close":
                self.file.close()
            raise OSError("simulated disk failure")
        return getattr(self.file, operation)(*args)

    def write(self, text):
        return self._call("write", text)

    def flush(self):
        return self._call("flush")

    def close(self):
        return self._call("close")


@pytest.mark.parametrize("name", ["events.jsonl", "notes.md"])
@pytest.mark.parametrize("operation", ["write", "flush", "close"])
def test_trace_file_failure_preserves_session_and_closes_other_files(
    mocker, name, operation
):
    fault = {"name": name, "operation": operation, "armed": False}
    opened = []

    def open_file(*args, **kwargs):
        file = builtins.open(*args, **kwargs)
        opened.append(file)
        return _FaultyFile(file, fault)

    mocker.patch("physiclaw.agent.trace.trace.open", open_file, create=True)
    trace = Trace("io-failure", Images("io-failure"))
    fault["armed"] = True

    try:
        persisted = trace.write(
            {
                "event": "tool_result",
                "turn": 0,
                "name": "note",
                "arguments": {"summary": "story"},
                "text": "noted",
            }
        )
        assert persisted is not (
            name == "events.jsonl" and operation in {"write", "flush"}
        )
        assert trace.write({"event": "request", "turn": 1, "message_count": 2}) is True
    finally:
        trace.close()

    directory = paths.sessions_dir() / "io-failure"
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    events = (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert fault["armed"] is False
    assert all(file.closed for file in opened)
    assert summary["turns"] == 2 and summary["tool_calls"] == {"note": 1}
    assert json.loads(events[-1])["event"] == "request"


def test_unserializable_event_reports_failure_and_preserves_following_events():
    trace = Trace("invalid-event", Images("invalid-event"))
    circular = []
    circular.append(circular)
    try:
        assert trace.write({"event": "evidence", "value": circular}) is False
        assert trace.write({"event": "request", "turn": 1}) is True
    finally:
        trace.close()


@pytest.mark.parametrize("raises", [False, True])
def test_tool_result_image_failure_is_acknowledged(mocker, raises):
    from physiclaw.contract.dto import ImageBlock

    images = Images("tool-image-failure")
    trace = Trace("tool-image-failure", images)
    mocker.patch.object(
        images,
        "put",
        return_value="",
        side_effect=OSError("disk full") if raises else None,
    )
    try:
        assert (
            trace.write(
                {
                    "event": "tool_result",
                    "turn": 0,
                    "name": "peek",
                    "blocks": [ImageBlock("image/jpeg", "ZnJhbWU=")],
                }
            )
            is False
        )
    finally:
        trace.close()


def test_trace_unavailable_notes_file_does_not_abort_session():
    images = Images("no-notes")
    directory = paths.sessions_dir() / "no-notes"
    (directory / "notes.md").mkdir()
    trace = Trace("no-notes", images)

    try:
        trace.write(
            {
                "event": "tool_result",
                "turn": 0,
                "name": "note",
                "arguments": {"summary": "story"},
                "text": "noted",
            }
        )
    finally:
        trace.close()

    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    assert summary["tool_calls"] == {"note": 1}
    assert not paths.active_session_marker().exists()


def _write_all(trace, events):
    for event in events:
        trace.write(event)


@pytest.mark.parametrize("turns, expected", [([], None), ([0], 0), ([3, 1], 3)])
def test_usage_records_follow_the_highest_started_turn(turns, expected, caplog):
    caplog.set_level("INFO", logger="physiclaw.agent.trace")
    trace = Trace("usage-turn", Images("usage-turn"))
    _write_all(trace, [{"event": "request", "turn": turn} for turn in turns])

    try:
        trace.write({"event": "usage", "turn": 99, "model": "test/model", "input": 5})
    finally:
        trace.close()

    lines = (
        (paths.sessions_dir() / "usage-turn" / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    record = json.loads(lines[-1])
    assert record["turn"] == expected
    assert "usage[? test/model]" in caplog.text and "in=5" in caplog.text


def test_empty_session_summary_preserves_schema_defaults():
    with freeze_time("2026-10-14T12:00:00"):
        trace = Trace("empty", Images("empty"))

        trace.close()

    summary = json.loads(
        (paths.sessions_dir() / "empty" / "summary.json").read_text(encoding="utf-8")
    )
    assert summary["started_at"] == "2026-10-14T12:00:00.000"
    assert summary["model_ref"] == summary["prompt_hash"] == ""
    assert summary["triggers"] == []
    assert summary["outcome"] == {"sentinel": None, "recap": "", "crashed": False}
    assert summary["turns"] == 0
    assert "event" not in summary["env"]


def test_summary_adds_repeated_calls_without_billing_plugin_turns():
    with freeze_time("2026-10-14T12:00:00") as clock:
        trace = Trace("calls", Images("calls"))

        _write_all(
            trace,
            [
                {"event": "response", "turn": 0, "elapsed_ms": 17},
                {
                    "event": "usage",
                    "call": "turn",
                    "model": "fake/model",
                    "elapsed_ms": 17,
                },
                {"event": "response", "turn": 1, "elapsed_ms": 23},
                {
                    "event": "usage",
                    "call": "turn",
                    "model": "fake/model",
                    "elapsed_ms": 23,
                },
                {
                    "event": "response",
                    "turn": 2,
                    "producer": {"plugin_id": "conductor-slot"},
                    "presentation": "playbook",
                    "elapsed_ms": 100,
                },
                {
                    "event": "response",
                    "turn": 3,
                    "producer": {"plugin_id": "conductor-slot"},
                    "presentation": "playbook",
                    "elapsed_ms": 200,
                },
                {"event": "micro_call"},
                {"event": "micro_call"},
            ],
        )
        clock.move_to("2026-10-14T12:00:02.500")
        trace.close()

    summary = json.loads(
        (paths.sessions_dir() / "calls" / "summary.json").read_text(encoding="utf-8")
    )
    assert (
        summary["provider_calls"]
        == summary["conductor_turns"]
        == summary["micro_calls"]
        == 2
    )
    assert summary["provider_time_ms"] == 40
    assert summary["duration_s"] == 2.5


def test_host_producer_accounting_separates_plugins_decisions_and_llm_calls():
    trace = Trace("host-producers", Images("host-producers"))
    trace.write(
        {
            "event": "response",
            "turn": 0,
            "producer": {"plugin_id": "decision-slot"},
            "presentation": "plugin",
            "elapsed_ms": 200,
        }
    )
    trace.write(
        {
            "event": "response",
            "turn": 1,
            "producer": {"plugin_id": None},
            "presentation": "model",
            "elapsed_ms": 50,
        }
    )
    trace.write(
        {
            "event": "response",
            "turn": 2,
            "producer": {"plugin_id": "playbook-slot"},
            "presentation": "playbook",
            "elapsed_ms": 2,
        }
    )
    trace.write(
        {
            "event": "response",
            "turn": 3,
            "producer": {"plugin_id": "playbook-slot"},
            "presentation": "playbook",
            "elapsed_ms": 3,
        }
    )
    trace.write(
        {
            "event": "response",
            "turn": 4,
            "producer": {"plugin_id": None},
            "presentation": "model",
            "elapsed_ms": 40,
        }
    )
    for elapsed in (50, 40):
        trace.write(
            {
                "event": "usage",
                "call": "turn",
                "model": "fake/model",
                "elapsed_ms": elapsed,
            }
        )
    trace.write({"event": "micro_call"})
    decision = {
        "event": "decision_call",
        "turn": 0,
        "model": "vendor/bounded",
        "elapsed_ms": 180,
        "usage": {"input_tokens": 120, "output_tokens": None},
    }
    trace.write(decision)
    trace.close()

    directory = paths.sessions_dir() / "host-producers"
    summary = json.loads((directory / "summary.json").read_text())
    assert summary["plugin_turns"] == 1
    assert summary["provider_calls"] == summary["conductor_turns"] == 2
    assert summary["provider_time_ms"] == 90
    assert summary["micro_calls"] == summary["decision_calls"] == 1
    assert summary["decision_time_ms"] == 180
    assert summary["usage"]["input_tokens"] == summary["usage"]["output_tokens"] == 0
    assert "vendor/bounded" not in summary["usage"]["by_model"]
    saved = [
        json.loads(line)
        for line in (directory / "events.jsonl").read_text().splitlines()
    ]
    assert (
        next(e for e in saved if e["event"] == "decision_call")["usage"]
        == decision["usage"]
    )
    assert (directory / "notes.md").read_text().strip().endswith("note history")


@pytest.mark.parametrize(
    "event",
    [
        "tool_error",
        "tool_invalid_args",
        "tool_unknown",
        "provider_failed",
        "crashed",
        "budget_exhausted",
        "finish_length_warning",
    ],
)
def test_failure_events_are_flagged_in_the_viewer(event):
    trace = Trace("warning", Images("warning"))

    try:
        trace.write({"event": event, "turn": 0, "name": "tap"})
    finally:
        trace.close()

    model = viewer.load(paths.sessions_dir() / "warning")
    row = model["groups"][-1]["records"][-1]
    assert row["warn"] is True
    assert isinstance(row["words"], str) and row["words"]


@pytest.mark.parametrize("event", ["response", "tool_result", "tool_error"])
def test_zero_elapsed_time_does_not_inflate_summary(event):
    trace = Trace("zero-time", Images("zero-time"))

    try:
        trace.write({"event": event, "turn": 0, "name": "tap", "elapsed_ms": 0})
    finally:
        trace.close()

    summary = json.loads(
        (paths.sessions_dir() / "zero-time" / "summary.json").read_text(
            encoding="utf-8"
        )
    )
    assert summary["provider_time_ms"] == summary["tool_time_ms"] == 0


def test_walk_summary_keeps_payload_without_event_envelope():
    trace = Trace("walk-summary", Images("walk-summary"))
    payload = {"app": "demo", "playbook": "login", "outcome": "done", "node": "home"}

    try:
        trace.write({"event": "walk", "t": "2026-10-14T12:00:00.000", **payload})
    finally:
        trace.close()

    summary = json.loads(
        (paths.sessions_dir() / "walk-summary" / "summary.json").read_text(
            encoding="utf-8"
        )
    )
    assert summary["walks"] == [payload]


def test_image_inventory_failure_keeps_session_summary(mocker):
    trace = Trace("unreadable-images", Images("unreadable-images"))
    mocker.patch.object(Path, "glob", side_effect=OSError("unreadable directory"))

    trace.close()

    summary = json.loads(
        (paths.sessions_dir() / "unreadable-images" / "summary.json").read_text(
            encoding="utf-8"
        )
    )
    assert summary["images"] == 0
    assert not paths.active_session_marker().exists()


def test_model_accounting_includes_retries_without_counting_proposals_twice():
    trace = Trace("model-retries", Images("model-retries"))
    trace.write({"event": "turn_started", "turn": 0})
    for elapsed, error in [(40, "timeout"), (60, None), (90, None)]:
        trace.write(
            {
                "event": "usage",
                "call": "turn",
                "model": "fake/model",
                "elapsed_ms": elapsed,
                "error": error,
            }
        )
    trace.write(
        {"event": "response", "turn": 0, "presentation": "model", "elapsed_ms": 1100}
    )
    trace.close()

    summary = json.loads(
        (paths.sessions_dir() / "model-retries" / "summary.json").read_text()
    )
    assert summary["provider_calls"] == 3
    assert summary["provider_time_ms"] == 190
    assert summary["usage"]["by_model"]["fake/model"]["calls"] == 3
    assert summary["usage"]["by_model"]["fake/model"]["failed"] == 1
