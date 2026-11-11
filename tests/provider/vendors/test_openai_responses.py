"""Reject corrupt Responses items before accepting an answer or replaying it."""

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from physiclaw.common.provider_errors import ProviderTransientError
from physiclaw.contract.dto import AssistantMessage, FinishReason, ToolCall
from physiclaw.contract.structured import OutputSpec
from physiclaw.provider.chat.structured import decode_completion
from physiclaw.provider.vendors.openai.responses import encode_message, parse_response


def function_call(**overrides):
    return {
        "type": "function_call",
        "call_id": "call-1",
        "name": "tap",
        "arguments": '{"bbox":[0.1,0.2,0.3,0.4]}',
        **overrides,
    }


@pytest.fixture(
    params=[
        ([None], "invalid Responses output item"),
        (
            [function_call(), function_call(name="swipe")],
            "duplicate function call_id",
        ),
        ([function_call(status="in_progress")], "incomplete function call"),
        ([function_call(arguments={})], "function arguments must be JSON text"),
        (
            [
                {
                    "type": "message",
                    "role": "assistant",
                    "status": "incomplete",
                    "content": [],
                }
            ],
            "incomplete assistant message",
        ),
    ],
    ids=[
        "invalid-item",
        "duplicate-id",
        "unfinished-call",
        "non-json-args",
        "unfinished-message",
    ],
)
def damaged_reply(request):
    items, error = request.param
    valid_answer = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": '{"ok":true}'}],
    }
    return {
        "status": "completed",
        "output": [valid_answer, *items],
        "usage": {"input_tokens": 17, "output_tokens": 9},
    }, error


def test_parse_response_damaged_chat_rejects_partial_success_and_keeps_usage(
    damaged_reply,
):
    raw, message = damaged_reply

    with pytest.raises(ProviderTransientError, match=message) as caught:
        parse_response(raw, model="test-model", structured=False)

    assert caught.value.reply.usage.prompt_tokens == 17
    assert caught.value.reply.usage.completion_tokens == 9
    assert caught.value.reply.vendor_extra == {}


def test_parse_response_damaged_structured_reply_cannot_accept_valid_json(
    damaged_reply,
):
    raw, message = damaged_reply
    spec = OutputSpec("answer", {"type": "object", "properties": {}}, dict)

    reply = parse_response(raw, model="test-model", structured=True)
    completion = decode_completion(reply, spec, transport="native")

    assert completion.value is None
    assert completion.failure.kind == "format"
    assert completion.failure.message == message
    assert reply.vendor_extra == {}
    assert reply.usage.completion_tokens == 9


@pytest.mark.parametrize("raw", [None, [], "not an object", 42])
@pytest.mark.parametrize("structured", [False, True])
def test_parse_response_non_object_envelope_is_a_retryable_failure(raw, structured):
    with pytest.raises(ProviderTransientError, match="expected object"):
        parse_response(raw, model="test-model", structured=structured)


json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=30),
    lambda values: (
        st.lists(values, max_size=4)
        | st.dictionaries(st.text(max_size=12), values, max_size=4)
    ),
    max_leaves=12,
)


@given(arguments=st.dictionaries(st.text(max_size=12), json_values, max_size=4))
def test_response_function_arguments_round_trip_without_loss_or_mutation(arguments):
    call = ToolCall("call-1", "custom", arguments)
    original = json.dumps(arguments, sort_keys=True)
    message = AssistantMessage("", [call], FinishReason.TOOL_CALLS)
    wire = encode_message(message, model="test-model")

    reply = parse_response(
        {"status": "completed", "output": wire}, model="test-model", structured=False
    )

    assert reply.tool_calls == [call]
    assert encode_message(reply, model="test-model") == wire
    assert json.dumps(arguments, sort_keys=True) == original
