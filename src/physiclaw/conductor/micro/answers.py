"""Reading a reply — strict JSON, validated against what the call
declares (`calltable.SPECS`): a question's allowed answers, or a
move's granted tools and each tool's own arguments — so a hallucinated
option is impossible rather than unlikely. Anything else is `MISSING`,
and the channel asks once more before the walk hands over.
"""

from typing import Any, Callable

from physiclaw.common.bbox import parse_box
from physiclaw.common.text import json_span
from physiclaw.conductor.micro.calltable import SPECS, macro_key
from physiclaw.conductor.micro.decision import PARSE_TASK
from physiclaw.conductor.spec.calls import (
    ACTION,
    ACTION_WORDS,
    ARGS,
    AT,
    DIRECTION,
    LABEL,
    NAME,
    SCROLL_ARMS,
    TOOL_ARGS,
)


def parse_reply(
    text: str, allowed: tuple[str, ...], call: str = PARSE_TASK
) -> tuple[tuple[str, str, float, dict[str, Any]] | None, str]:
    """Strict JSON-object parse + the constraint tax, for one call's
    row. Returns ((word, reason, confidence, whole object), "") or
    (None, error) — the word is the reply's field (`answer` for a
    question, `action` for a move); the object rides along so the row's
    outcome mapper can read the fields beside it (parse_task's
    `inputs`, a tool call's `args`). A tool call is judged whole against
    its tool's arguments (`TOOL_ARGS` through `_ARG_CHECKS`); a granted
    macro is a kind-tagged entry of `allowed` (`macro_key`)."""
    obj = json_span(text, "{", "}")
    if not isinstance(obj, dict):
        return None, "no JSON object found"
    field = SPECS[call].field
    word = obj.get(field)
    if not isinstance(word, str) or word not in allowed:
        return None, f"{field} must be exactly one of the allowed values"
    if field == ACTION:
        if word not in ACTION_WORDS:
            return None, f"{field} must be exactly one of the allowed values"
        args = obj.get(ARGS, {})
        if not isinstance(args, dict):
            return None, f'"{ARGS}" must be an object with the tool\'s arguments'
        err = _check_action(word, args, allowed)
        if err:
            return None, err
    reason = obj.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return None, "a non-empty reason is required"
    confidence = obj.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0.0 <= float(confidence) <= 1.0
    ):
        return None, "confidence must be a number between 0 and 1"
    return (word, reason.strip(), float(confidence), obj), ""


# Each argument's hint (the repair message's words, beside the legend's)
# and its check — "" when the value fits, MISSING when it is absent or
# not even the right shape, else why not. `_check_action` walks a
# tool's `TOOL_ARGS` through this table, so a new argument gets its
# validation the moment it is named.
MISSING = "missing"


def _check_label(value: Any, allowed: tuple[str, ...]) -> str:
    return "" if isinstance(value, str) and value.strip() else MISSING


def _check_box(value: Any, allowed: tuple[str, ...]) -> str:
    if not isinstance(value, list):
        return MISSING
    try:
        parse_box(value)  # the engine's own rules, bools refused
    except ValueError as e:
        return str(e)
    return ""


def _check_direction(value: Any, allowed: tuple[str, ...]) -> str:
    return "" if isinstance(value, str) and value in SCROLL_ARMS else MISSING


def _check_macro(value: Any, allowed: tuple[str, ...]) -> str:
    return "" if isinstance(value, str) and macro_key(value) in allowed else MISSING


_ARG_CHECKS: dict[str, tuple[str, Callable[[Any, tuple[str, ...]], str]]] = {
    LABEL: (
        "what the box is — its on-screen text, or a short description",
        _check_label,
    ),
    AT: ("a box [left, top, right, bottom]", _check_box),
    DIRECTION: ('"down" or "up"', _check_direction),
    NAME: ("a granted macro name", _check_macro),
}


def _check_action(action: str, args: dict, allowed: tuple[str, ...]) -> str:
    """A tool call's args against its tool: every key `TOOL_ARGS` lists,
    present and fitting. Extra keys are ignored. "" when the args fit."""
    for key in TOOL_ARGS.get(action, ()):
        hint, check = _ARG_CHECKS[key]
        err = check(args[key], allowed) if key in args else MISSING
        if err == MISSING:
            return f"{action} needs args.{key}: {hint}"
        if err:
            return err
    return ""
