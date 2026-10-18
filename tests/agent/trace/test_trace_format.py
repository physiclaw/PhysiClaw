"""Public formatting contracts shared by the daily log and session viewer.

Accepted mutation survivors change presentation wording, missing-field
placeholders, or a display truncation limit by one character. These do not
remove diagnostic fields, actions, or warning tones. We also accept weaker
static roster assertions that are equivalent for the current vocabulary.
"""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from physiclaw.agent.trace import viewer
from physiclaw.agent.trace.format import (
    brief,
    describe_call,
    fmt_tokens,
    note_text,
    session_footer,
    summarize_event,
    usage_summary,
    usage_text,
)


@given(st.text(max_size=1000), st.integers(min_value=1, max_value=200))
def test_brief_limits_text_without_changing_its_prefix(text, limit):
    result = brief(text, limit)

    assert len(result) <= limit
    assert result == text or (result.endswith("…") and text.startswith(result[:-1]))


@pytest.mark.parametrize(
    "count, expected",
    [
        (0, "0"),
        (999, "999"),
        (1000, "1.0k"),
        (999999, "1000.0k"),
        (1000000, "1.0M"),
        (1350000, "1.4M"),
    ],
)
def test_token_format_boundaries(count, expected):
    result = fmt_tokens(count)

    assert result == expected


@pytest.mark.parametrize("status", ["DONE", "STUCK", "WAIT"])
def test_final_result_keeps_receipts_and_required_actions_after_a_long_recap(status):
    recap = (
        "Verified item and delivery details. " * 12
        + "Order 12345: payment still needs approval."
    )
    summary = {
        "outcome": {"sentinel": status, "crashed": False, "recap": recap},
        "duration_s": 12.0,
    }
    footer = session_footer(summary)
    assert footer.endswith(recap)
    assert "\n" not in footer


@pytest.mark.parametrize("status", ["DONE", "FAIL", "WAIT", "IDLE", "STUCK"])
def test_final_result_does_not_repeat_a_status_only_recap(status):
    summary = {
        "outcome": {
            "sentinel": status,
            "crashed": False,
            "recap": status.lower() + ".",
        },
        "duration_s": 1.0,
    }
    assert " — " not in session_footer(summary)


@pytest.mark.parametrize(
    "name, arguments, expected",
    [
        ("tap", {"bbox": [0.1, 0.2, 0.3, 0.4]}, "Tapped [0.1, 0.2, 0.3, 0.4]"),
        ("swipe", {"direction": "left"}, "Swiped left"),
        ("wait", {"seconds": 3}, "Waited 3 s"),
        (
            "double_tap",
            {"bbox": [0.1, 0.2, 0.3, 0.4]},
            "Double-tapped [0.1, 0.2, 0.3, 0.4]",
        ),
        (
            "long_press",
            {"bbox": [0.1, 0.2, 0.3, 0.4]},
            "Long-pressed [0.1, 0.2, 0.3, 0.4]",
        ),
        (
            "send_to_clipboard",
            {"text": "hello"},
            "Copied to the phone's clipboard: hello",
        ),
        ("run_macro", {"name": "demo/flow"}, "Ran macro demo/flow"),
        ("note", {"summary": "conductor: at home"}, "Noted: at home"),
        ("update_progress", {}, "Updated the plan"),
        ("end_session", {"status": "DONE"}, "Ended the session: DONE"),
        ("append_log", {}, "Wrote to the log"),
        ("finish_job", {"id": "job-123"}, "Finished job job-123"),
        ("Skill", {"name": "login"}, "Loaded skill login"),
        ("future_tool", {"count": 3}, "future_tool(count=3)"),
        ("future_tool", None, "future_tool()"),
    ],
)
def test_call_description_keeps_action_and_arguments(name, arguments, expected):
    result = describe_call(name, arguments)

    assert result == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("conductor: at home", "at home"),
        ("at home", "at home"),
        ("check conductor: at home", "check conductor: at home"),
    ],
)
def test_note_text_only_removes_leading_author_prefix(text, expected):
    result = note_text({"summary": text})

    assert result == expected


def test_usage_text_includes_reasoning_and_failure_when_present():
    event = {
        "call": "micro",
        "model": "test/model",
        "input": 8,
        "input_new": 3,
        "cache_read": 4,
        "cache_write": 1,
        "output": 2,
        "reasoning": 1,
        "elapsed_ms": 250,
        "error": "TimeoutError",
    }

    result = usage_text(event)

    assert (
        result
        == "usage[micro test/model] 0.2s in=8 (read 4, write 1, new 3) out=2 (reasoning 1) error=TimeoutError"
    )


def test_usage_text_does_not_invent_usage_for_absent_buckets():
    result = usage_text({"elapsed_ms": 1350})

    assert result == "usage[? ?] 1.4s in=0 (read 0, write 0, new 0) out=0"


@pytest.mark.parametrize(
    "event, expected",
    [
        (
            {"input": 12400, "output": 320, "cache_read": 9920},
            "Tokens: 12.4k in / 320 out · 80% cached",
        ),
        ({"input": 500, "output": 120}, "Tokens: 500 in / 120 out"),
        ({"input": 1200000, "output": 2500}, "Tokens: 1.2M in / 2.5k out"),
        (
            {"input": 100, "output": 0, "cache_read": 200},
            "Tokens: 100 in / 0 out · 100% cached",
        ),
        ({}, "Tokens: not reported"),
        ({"error": "TimeoutError", "input": 0, "output": 0}, "Tokens: not reported"),
        (
            {"call": "turn", "turn": 3, "input": 100, "output": 20},
            "Tokens: 100 in / 20 out · step 3",
        ),
        (
            {"call": "micro", "input": 100, "output": 20},
            "Tokens: 100 in / 20 out · decision",
        ),
        (
            {"call": "curate", "input": 100, "output": 20},
            "Tokens: 100 in / 20 out · lesson upkeep",
        ),
    ],
)
def test_usage_summary_is_short_and_does_not_invent_missing_counts(event, expected):
    assert usage_summary(event) == expected


def test_usage_summary_can_identify_an_alternate_model():
    assert (
        usage_summary(
            {"model": "test/decision", "input": 100, "output": 20}, include_model=True
        )
        == "Tokens: 100 in / 20 out · test/decision"
    )
    assert (
        usage_summary({"model": "test/decision"}, include_model=True)
        == "Tokens: not reported · test/decision"
    )


@pytest.mark.parametrize(
    "event, markers",
    [
        (
            {
                "event": "wake",
                "session": "abc123",
                "model_ref": "test/model",
                "triggers": [{"source": "schedule"}, {"source": "manual"}],
            },
            ["abc123", "test/model", "schedule", "manual"],
        ),
        (
            {
                "event": "env",
                "physiclaw": "0.4.9",
                "python": "3.14.1",
                "platform": "Darwin",
                "host": "test-host",
                "utc_offset": "+08:00",
            },
            ["0.4.9", "3.14.1", "Darwin", "test-host", "+08:00"],
        ),
        (
            {
                "event": "tool_result",
                "name": "tap",
                "arguments": {"bbox": [0.1, 0.2, 0.3, 0.4]},
                "text": "tapped",
            },
            ["tap", "bbox", "0.1", "0.2", "0.3", "0.4", "tapped"],
        ),
        (
            {
                "event": "prefix_drift",
                "expected": "123abc456def789",
                "actual": "fed321cba654987",
            },
            ["123abc456def", "fed321cba654"],
        ),
    ],
)
def test_event_summary_preserves_diagnostic_fields(event, markers):
    result = summarize_event(event)

    assert isinstance(result, str)
    assert all(marker in result for marker in markers)


def test_viewer_vocabulary_covers_calls_and_outcome_tones(tmp_path):
    words = viewer.load(tmp_path)["words"]

    assert set(words["calls"]) == {"parse_task", "agent_fields", "agent_act"}
    assert all(isinstance(value, str) and value for value in words["calls"].values())
    assert {key: value["tone"] for key, value in words["sentinels"].items()} == {
        "DONE": "ok",
        "WAIT": "amber",
        "IDLE": "",
        "STUCK": "bad",
        "FAIL": "bad",
    }
    assert all(
        isinstance(value["words"], str) and value["words"]
        for value in words["sentinels"].values()
    )
