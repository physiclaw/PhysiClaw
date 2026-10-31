"""Untrusted micro replies must fit their grants and reject malformed arguments."""

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from physiclaw.conductor.micro.answers import parse_reply
from physiclaw.conductor.micro.decision import AGENT_ACT


@pytest.mark.parametrize(
    "confidence",
    [10**400, -(10**400), True, None, "0.9", float("nan"), float("inf")],
)
def test_parse_reply_invalid_confidence_returns_repair_error(confidence) -> None:
    text = json.dumps(
        {"answer": "task", "reason": "requested", "confidence": confidence}
    )

    parsed, error = parse_reply(text, ("task",))

    assert parsed is None
    assert error == "confidence must be a number between 0 and 1"


@given(confidence=st.one_of(st.integers(max_value=-1), st.integers(min_value=2)))
def test_parse_reply_out_of_range_integers_never_raise(confidence: int) -> None:
    text = json.dumps(
        {"answer": "task", "reason": "requested", "confidence": confidence}
    )

    parsed, error = parse_reply(text, ("task",))

    assert parsed is None
    assert error == "confidence must be a number between 0 and 1"


@given(confidence=st.floats(min_value=0, max_value=1))
def test_parse_reply_valid_confidence_round_trips(confidence: float) -> None:
    text = json.dumps(
        {"answer": "task", "reason": "requested", "confidence": confidence}
    )

    parsed, error = parse_reply(text, ("task",))

    assert parsed is not None
    assert parsed[:3] == ("task", "requested", confidence)
    assert error == ""


@pytest.mark.parametrize("text", ["", "not JSON", "{}{}", '{"answer":'])
def test_malformed_json_returns_a_repair_error(text: str) -> None:
    assert parse_reply(text, ("task",)) == (None, "no JSON object found")


@pytest.mark.parametrize("word", [None, True, [], "unknown", "TASK", " task "])
def test_answer_must_exactly_match_the_granted_space(word) -> None:
    text = json.dumps({"answer": word, "reason": "requested", "confidence": 1})

    parsed, error = parse_reply(text, ("task",))

    assert parsed is None
    assert error == "answer must be exactly one of the allowed values"


@pytest.mark.parametrize("reason", [None, [], True, "", " \n\t"])
def test_reply_requires_a_nonempty_text_reason(reason) -> None:
    text = json.dumps({"answer": "task", "reason": reason, "confidence": 1})

    assert parse_reply(text, ("task",)) == (None, "a non-empty reason is required")


def test_a_macro_grant_cannot_be_used_as_the_action_verb() -> None:
    text = json.dumps({"action": "macro:open", "reason": "requested", "confidence": 1})

    parsed, error = parse_reply(text, ("run_macro", "macro:open"), AGENT_ACT)

    assert parsed is None
    assert error == "action must be exactly one of the allowed values"


@pytest.mark.parametrize("args", [None, [], "", True, 1])
def test_action_arguments_must_be_an_object_even_for_a_zero_argument_tool(args) -> None:
    text = json.dumps({"action": "back", "args": args, "reason": "r", "confidence": 1})

    parsed, error = parse_reply(text, ("back",), AGENT_ACT)

    assert parsed is None
    assert error == '"args" must be an object with the tool\'s arguments'


@pytest.mark.parametrize(
    "action, args, fragment",
    [
        ("tap", {}, "tap needs args.label"),
        ("tap", {"label": " "}, "tap needs args.label"),
        ("tap", {"label": 1}, "tap needs args.label"),
        ("tap", {"label": "buy"}, "tap needs args.at"),
        ("tap", {"label": "buy", "at": "0,0,1,1"}, "tap needs args.at"),
        ("tap", {"label": "buy", "at": [0, 0, 1]}, "bbox:"),
        ("tap", {"label": "buy", "at": [0, 0, True, 1]}, "bbox:"),
        ("tap", {"label": "buy", "at": [0, 0, 10**400, 1]}, "bbox:"),
        ("tap", {"label": "buy", "at": [0, 0, 0, 1]}, "bbox:"),
        ("scroll", {}, "scroll needs args.direction"),
        ("scroll", {"direction": "left"}, "scroll needs args.direction"),
        ("scroll", {"direction": []}, "scroll needs args.direction"),
        ("run_macro", {}, "run_macro needs args.name"),
        ("run_macro", {"name": "ungranted"}, "run_macro needs args.name"),
        ("run_macro", {"name": []}, "run_macro needs args.name"),
    ],
)
def test_invalid_tool_arguments_cannot_escape_the_reply_parser(
    action, args, fragment
) -> None:
    text = json.dumps(
        {"action": action, "args": args, "reason": "requested", "confidence": 1}
    )

    parsed, error = parse_reply(text, (action, "macro:open"), AGENT_ACT)

    assert parsed is None
    assert error.startswith(fragment)


@pytest.mark.parametrize(
    "action, args",
    [
        ("tap", {"label": "buy", "at": [0, 0, 1, 1]}),
        ("scroll", {"direction": "up"}),
        ("scroll", {"direction": "down"}),
        ("run_macro", {"name": "open"}),
        ("back", {}),
        ("done", {"result": "found"}),
    ],
)
def test_valid_tool_arguments_survive_parsing_without_changes(action, args) -> None:
    obj = {"action": action, "args": args, "reason": " requested ", "confidence": 1}

    parsed, error = parse_reply(json.dumps(obj), (action, "macro:open"), AGENT_ACT)

    assert error == ""
    assert parsed == (action, "requested", 1.0, obj)
