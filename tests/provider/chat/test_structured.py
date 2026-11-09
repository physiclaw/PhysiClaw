"""Strict decoding and typed rejection of completed generations."""

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from physiclaw.contract.dto import AssistantMessage, FinishReason, ToolCall
from physiclaw.contract.structured import OutputSpec
from physiclaw.provider.chat.structured import decode_completion, strict_json


@pytest.mark.parametrize(
    "text", ['{"x":1,"x":2}', '{"x":NaN}', "[Infinity]", "{} garbage"]
)
def test_strict_json_invalid_input_rejected(text):
    with pytest.raises(
        ValueError, match="duplicate JSON key|invalid JSON constant|Extra data"
    ):
        strict_json(text)


@given(
    st.dictionaries(
        st.text(), st.one_of(st.none(), st.text(), st.booleans(), st.integers())
    )
)
def test_strict_json_roundtrip(value):
    text = json.dumps(value)

    result = strict_json(text)

    assert result == value


@pytest.mark.parametrize(
    "calls",
    [
        [],
        [ToolCall("a", "wrong", {})],
        [ToolCall("", "submit_turn", {})],
        [ToolCall("a", "submit_turn", {}), ToolCall("b", "submit_turn", {})],
    ],
)
def test_decode_completion_requires_one_named_envelope(calls):
    reply = AssistantMessage("", calls, FinishReason.TOOL_CALLS)

    result = decode_completion(reply, OutputSpec("sample", {}, dict))

    assert result.failure.kind == "format"
    assert result.reply is reply


@pytest.mark.parametrize(
    "content,calls,expected",
    [
        ("{}", [], None),
        ("bad", [], "format"),
        ("{}", [ToolCall("a", "submit_turn", {})], "format"),
    ],
)
def test_decode_completion_native_shape(content, calls, expected):
    reply = AssistantMessage(content, calls, FinishReason.STOP)

    result = decode_completion(
        reply, OutputSpec("sample", {}, dict), transport="native"
    )

    assert (result.failure.kind if result.failure else None) == expected


@pytest.mark.parametrize(
    "finish,refused,error,kind,message",
    [
        (
            FinishReason.CONTENT_FILTER,
            False,
            None,
            "refusal",
            "Provider refused this generation",
        ),
        (FinishReason.STOP, True, None, "refusal", "Provider refused this generation"),
        (
            FinishReason.LENGTH,
            False,
            None,
            "truncation",
            "Provider truncated the output; nothing was accepted",
        ),
        (FinishReason.STOP, False, "malformed reply", "format", "malformed reply"),
    ],
)
def test_decode_completion_preserves_typed_rejection(
    finish, refused, error, kind, message
):
    reply = AssistantMessage("", [], finish, refused=refused, decode_error=error)

    result = decode_completion(reply, OutputSpec("sample", {}, dict))

    assert result.failure.kind == kind
    assert result.failure.message == message
    assert result.value is None
    assert result.reply is reply


def test_decode_completion_success_keeps_original_reply_and_value():
    reply = AssistantMessage(
        "", [ToolCall("envelope", "submit_turn", {"x": True})], FinishReason.TOOL_CALLS
    )

    result = decode_completion(reply, OutputSpec("sample", {}, dict))

    assert result.reply is reply
    assert result.failure is None
    assert result.value == {"x": True}


@pytest.mark.parametrize("transport", ["tool", "native"])
def test_failed_generation_cannot_be_accepted_even_with_valid_output(transport):
    reply = AssistantMessage(
        '{"x": true}',
        [ToolCall("failed", "submit_turn", {"x": True})] if transport == "tool" else [],
        FinishReason.ERROR,
    )

    result = decode_completion(
        reply, OutputSpec("sample", {}, dict), transport=transport
    )

    assert result.value is None
    assert result.failure.message == "Provider generation failed"
    assert result.reply is reply


@pytest.mark.parametrize(
    "transport,calls,expected",
    [
        ("tool", [], "Expected exactly one output function call"),
        (
            "tool",
            [ToolCall("a", "wrong", {})],
            "Expected submit_turn with a provider call ID",
        ),
        (
            "native",
            [ToolCall("a", "submit_turn", {})],
            "Native output must not contain tool calls",
        ),
    ],
)
def test_decode_completion_diagnostic_identifies_required_correction(
    transport, calls, expected
):
    reply = AssistantMessage("{}", calls, FinishReason.STOP)

    result = decode_completion(
        reply, OutputSpec("sample", {}, dict), transport=transport
    )

    assert result.failure.message == expected


@pytest.mark.parametrize(
    "value,expected",
    [('{"x":1,"x":2}', "duplicate JSON key: x"), ("NaN", "invalid JSON constant: NaN")],
)
def test_strict_json_diagnostic_identifies_invalid_token(value, expected):
    with pytest.raises(ValueError) as error:  # noqa: PT011 -- exact message is asserted below.
        strict_json(value)

    assert str(error.value) == expected
