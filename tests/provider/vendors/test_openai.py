"""Tests for `physiclaw.provider.vendors.openai.chat` — declaration.

Native OpenAI endpoint with provider-owned thinking request options.
"""

from __future__ import annotations

import json

import httpx
import pytest

from physiclaw.contract.dto import (
    FinishReason,
    ImageBlock,
    OutputResultMessage,
    TextBlock,
    ToolResultMessage,
    UserMessage,
)
from physiclaw.provider.vendors.openai.chat import OpenAIChatProvider


def response(text="{}", **extra):
    return {
        "id": "resp-test",
        "model": "gpt-6.1-sol",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "id": "msg-test",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
        **extra,
    }


@pytest.fixture(autouse=True)
def _stub_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")


def test_provider_id_pinned() -> None:
    """Provider id is the API surface (`openai`), matching the OpenClaw
    / Anthropic-SDK convention — not the brand (`chatgpt`)."""
    assert OpenAIChatProvider.PROVIDER_ID == "openai"


def test_base_url_pinned() -> None:
    assert OpenAIChatProvider.BASE_URL == "https://api.openai.com/v1"


def test_tool_images_stay_in_paired_responses_receipt():
    provider = OpenAIChatProvider(model="gpt-5")
    content = [TextBlock("observed"), ImageBlock("image/jpeg", "aW1hZ2U=")]
    result = ToolResultMessage("original-call", content)
    assert provider.serialize_history([result]) == [
        {
            "type": "function_call_output",
            "call_id": "original-call",
            "output": [
                {"type": "input_text", "text": "observed"},
                {"type": "input_image", "image_url": "data:image/jpeg;base64,aW1hZ2U="},
            ],
        }
    ]
    assert result.content == content


# ---------- thinking table ----------


def test_gpt5_takes_minimal_for_off_and_the_o_series_floors_at_low():
    p = OpenAIChatProvider(model="gpt-5")
    assert p.thinking_params("off") == {"reasoning": {"effort": "minimal"}}
    assert p.thinking_params("high") == {"reasoning": {"effort": "high"}}
    o = OpenAIChatProvider(model="o4-mini")
    assert o.thinking_params("off") == {"reasoning": {"effort": "low"}}
    assert o.thinking_params("medium") == {"reasoning": {"effort": "medium"}}


def test_a_non_reasoning_model_gets_no_field() -> None:
    assert OpenAIChatProvider(model="gpt-4o").thinking_params("off") == {}


@pytest.mark.parametrize(
    "model, effort",
    [
        ("gpt-5", "minimal"),
        ("gpt-5-mini-2025-08-07", "minimal"),
        ("gpt-5.1", "none"),
        ("gpt-5.1-2025-11-13", "none"),
        ("gpt-5.2", "none"),
        ("gpt-5.4", "none"),
        ("gpt-5.5", "none"),
        ("gpt-6-luna", "none"),
    ],
)
@pytest.mark.asyncio
async def test_chat_off_sends_effort_supported_by_model(model, effort, respx_mock):
    route = respx_mock.post("https://api.openai.com/v1/responses").mock(
        return_value=httpx.Response(
            200,
            json=response("ok"),
        )
    )
    provider = OpenAIChatProvider(model=model)

    try:
        await provider.chat(
            [UserMessage(content="hi")],
            [{"name": "probe", "input_schema": {"type": "object", "properties": {}}}],
            thinking="off",
        )
    finally:
        await provider.aclose()

    body = json.loads(route.calls.last.request.content)
    assert body["reasoning"]["effort"] == effort
    assert body["tools"][0]["name"] == "probe"


@pytest.mark.parametrize(
    "model", ["gpt-5.1-chat-latest", "gpt-5.2-pro", "gpt-5.1-codex", "gpt-5.99"]
)
def test_unknown_variant_keeps_its_api_default(model):
    provider = OpenAIChatProvider(model=model)

    params = provider.thinking_params("off")

    assert params == {}


async def test_native_output_keeps_prefix_stable_and_options_call_local(respx_mock):
    from physiclaw.contract.dto import OutputResultMessage, SystemMessage
    from physiclaw.contract.structured import OutputSpec

    provider = OpenAIChatProvider(model="gpt-6.1-sol")
    spec = OutputSpec("example", {"type": "object", "properties": {}}, dict)
    history = [SystemMessage("Host instructions"), UserMessage("begin")]
    route = respx_mock.post("https://api.openai.com/v1/responses").respond(
        json=response("{}")
    )
    try:
        result = await provider.generate_structured(history, spec)
        receipt = OutputResultMessage(result.reply, "done", is_error=True)
        first = json.loads(route.calls.last.request.content)
        second = provider.build_request(
            [*history, result.reply, receipt], [], output_spec=spec
        )
        ordinary = provider.build_request(history, [])
    finally:
        await provider.aclose()

    assert result.value == {} and result.reply.tool_calls == []
    assert first["input"][0] == second["input"][0]
    assert first["text"]["format"] == second["text"]["format"]
    assert all(
        m.get("role") != "tool" and "tool_calls" not in m for m in second["input"]
    )
    assert "(error)" in second["input"][-1]["content"][0]["text"]
    assert "text" not in ordinary and "tools" not in ordinary
    assert history[0].content == "Host instructions"
    assert "JSON object" not in json.dumps(ordinary)


@pytest.mark.parametrize("structured", [False, True])
async def test_cache_prefix_survives_screens_retries_and_compaction(structured):
    from copy import deepcopy

    from physiclaw.agent.engine import compact
    from physiclaw.contract.dto import CollapsePolicy, SystemMessage, ToolCall
    from physiclaw.contract.turn import TurnMetadata

    provider = OpenAIChatProvider(model="gpt-6.1-terra")
    history = [
        SystemMessage("stable instructions"),
        UserMessage("task"),
        UserMessage(compact.SUMMARY_INITIAL),
        UserMessage(compact.MEMORY_INITIAL),
        UserMessage(compact.SKILLS_INITIAL),
    ]
    previous_prefix = None
    folds = 0
    try:
        for turn in range(14):
            raw = response(model=provider.model)
            if structured:
                raw["output"][0]["id"] = f"msg-{turn}"
                raw["output"].insert(
                    0, {"type": "reasoning", "id": f"rs-{turn}", "summary": []}
                )
            else:
                raw["output"] = [
                    {
                        "type": "function_call",
                        "call_id": f"call-{turn}",
                        "name": "peek",
                        "arguments": "{}",
                    }
                ]
            reply = provider._decode_response(raw, structured=structured)
            reply.context_calls = [
                ToolCall(f"note-{turn}", "note", {"summary": f"observed {turn}"})
            ]
            # Mix screen and bookkeeping turns; native replay has multiple items.
            content = (
                "noted"
                if turn % 3 == 1
                else [TextBlock(f"screen {turn}"), ImageBlock("image/jpeg", "aW1hZ2U=")]
            )
            receipt = (
                OutputResultMessage(reply, content)
                if structured
                else ToolResultMessage(f"call-{turn}", content)
            )
            history.extend([reply, receipt])
            compact.drop_stale_screens(history)
            folded = compact.collapse_old_turns(
                history,
                policy=CollapsePolicy(first_at=6, keep=2, interval=4),
                collapsed_once=folds > 0,
            )
            folds += folded
            request = [*history, UserMessage(f"scratchpad {turn}")]
            original = deepcopy(request)
            metadata = TurnMetadata("test-session", turn, None, 20 - turn)
            wire = provider.serialize_history(request, metadata=metadata)
            assert provider.serialize_history(request, metadata=metadata) == wire
            assert request == original

            # Remove only cache annotations to compare the complete cached prefix.
            boundaries = []
            image_index = None
            for i, item in enumerate(wire):
                for block in item.get("output", item.get("content", [])):
                    if block["type"] == "input_image":
                        image_index = i
                    if "prompt_cache_breakpoint" in block:
                        assert block.pop("prompt_cache_breakpoint") == {
                            "mode": "explicit"
                        }
                        boundaries.append(i)
            stubs = sum(getattr(m, "is_superseded", False) for m in history)
            assert len(boundaries) == 1 + min(2, stubs)
            assert boundaries[0] == 0
            # The live image, scratchpad and metadata follow every breakpoint.
            assert image_index is not None
            assert boundaries[-1] < image_index < len(wire) - 2
            if previous_prefix is not None:
                assert wire[0] == previous_prefix[0]
                if not folded:
                    assert len(previous_prefix) - 1 in boundaries
                    assert wire[: len(previous_prefix)] == previous_prefix
                else:
                    assert wire[: len(previous_prefix)] != previous_prefix
            previous_prefix = deepcopy(wire[: boundaries[-1] + 1])
        assert folds == 3
    finally:
        await provider.aclose()


@pytest.mark.parametrize(
    "model",
    ["gpt-5.5", "gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-luna-2026-10-01", "gpt-6.99"],
)
def test_cache_protocol_matches_model_and_preserves_write_accounting(model):
    from physiclaw.contract.dto import SystemMessage

    provider = OpenAIChatProvider(model=model)
    body = provider.build_request(
        [SystemMessage("stable instructions"), UserMessage("variable tail")], []
    )
    explicit = model in {"gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-luna-2026-10-01"}
    assert ("prompt_cache_options" in body) is explicit
    assert ("prompt_cache_breakpoint" in json.dumps(body)) is explicit
    assert "cache_control" not in json.dumps(body)
    if explicit:
        assert body["prompt_cache_options"] == {"mode": "explicit"}
        assert "prompt_cache_breakpoint" not in json.dumps(body["input"][-1])
    from physiclaw.provider.vendors.openai.responses import parse_usage

    usage = parse_usage(
        {
            "usage": {
                "input_tokens": 100,
                "input_tokens_details": {
                    "cached_tokens": 40,
                    "cache_write_tokens": 30,
                },
            }
        }
    )
    assert (usage.prompt_tokens, usage.cached_tokens, usage.cache_creation_tokens) == (
        100,
        40,
        30,
    )


async def test_native_schema_failure_does_not_fall_back_to_tools(respx_mock):
    from physiclaw.common.provider_errors import ProviderConfigurationError
    from physiclaw.contract.structured import OutputSpec

    provider = OpenAIChatProvider(model="gpt-6.1-sol")
    spec = OutputSpec("example", {"type": "object", "additionalProperties": True}, dict)
    try:
        with pytest.raises(ProviderConfigurationError, match="additional properties"):
            await provider.generate_structured([], spec)
        assert provider.structured_transport == "native"
        assert not respx_mock.calls
    finally:
        await provider.aclose()


async def test_replays_opaque_items_only_to_same_model_and_keeps_usage(respx_mock):
    from copy import deepcopy

    from physiclaw.contract.structured import OutputSpec

    provider = OpenAIChatProvider(model="gpt-6.1-sol")
    other = OpenAIChatProvider(model="gpt-6.1-terra")
    raw = response(
        usage={
            "input_tokens": 13000,
            "output_tokens": 100,
            "input_tokens_details": {"cached_tokens": 10420, "cache_write_tokens": 400},
            "output_tokens_details": {"reasoning_tokens": 60},
        }
    )
    raw["output"].insert(
        0,
        {
            "type": "reasoning",
            "id": "rs-1",
            "encrypted_content": "opaque",
            "summary": [],
        },
    )
    raw["output"][1]["phase"] = "final_answer"
    original = deepcopy(raw)
    route = respx_mock.post("https://api.openai.com/v1/responses").respond(json=raw)
    try:
        completion = await provider.generate_structured(
            [UserMessage("start")],
            OutputSpec("sample", {"type": "object", "properties": {}}, dict),
        )
        reply = completion.reply
        followup = provider.serialize_history(
            [reply, OutputResultMessage(reply, "observed")]
        )
        foreign = other.serialize_history([reply])
    finally:
        await provider.aclose()
        await other.aclose()
    assert completion.value == {}
    assert reply.content == "{}"
    assert followup[:-1] == raw["output"]
    assert foreign == [{"role": "assistant", "content": "{}"}]
    assert reply.usage.buckets() == {
        "input": 13000,
        "input_new": 2180,
        "cache_read": 10420,
        "cache_write": 400,
        "output": 100,
        "reasoning": 60,
    }
    assert json.loads(route.calls[0].request.content)["store"] is False
    followup[0]["encrypted_content"] = "changed"
    assert reply.vendor_extra["openai"]["output"] == original["output"]
    assert raw == original


@pytest.mark.parametrize(
    "reason,kind",
    [
        ("max_output_tokens", "truncation"),
        ("content_filter", "refusal"),
        ("unknown", "format"),
    ],
)
async def test_incomplete_never_accepts_even_valid_json(reason, kind, respx_mock):
    from physiclaw.contract.structured import OutputSpec

    provider = OpenAIChatProvider(model="gpt-6.1-sol")
    respx_mock.post("https://api.openai.com/v1/responses").respond(
        json=response(
            status="incomplete",
            incomplete_details={"reason": reason},
            usage={"input_tokens": 10, "output_tokens": 20},
        )
    )
    try:
        result = await provider.generate_structured(
            [], OutputSpec("sample", {"type": "object", "properties": {}}, dict)
        )
    finally:
        await provider.aclose()
    assert result.value is None and result.failure.kind == kind
    assert result.reply.usage.completion_tokens == 20
    assert not result.reply.vendor_extra


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"status": []},
        {"status": "completed", "output": None},
        response(output=[{"type": "reasoning", "encrypted_content": "opaque"}]),
        response(output=[{"type": "message", "role": "user", "content": []}]),
        response(
            output=[
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": {}}],
                }
            ]
        ),
        response(output=[{"type": "unexpected"}]),
    ],
)
async def test_malformed_response_never_becomes_valid_turn(payload, respx_mock):
    from physiclaw.contract.structured import OutputSpec

    provider = OpenAIChatProvider(model="gpt-6.1-sol")
    route = respx_mock.post("https://api.openai.com/v1/responses").respond(json=payload)
    try:
        result = await provider.generate_structured(
            [], OutputSpec("sample", {"type": "object", "properties": {}}, dict)
        )
    finally:
        await provider.aclose()
    assert result.value is None and result.failure.kind == "format"
    assert route.call_count == 1


async def test_failed_generation_accounts_paid_usage(respx_mock):
    from unittest.mock import Mock

    from physiclaw.common.provider_errors import ProviderTransientError
    from physiclaw.contract.structured import OutputSpec

    sink = Mock()
    provider = OpenAIChatProvider(model="gpt-6.1-sol", usage_sink=sink)
    respx_mock.post("https://api.openai.com/v1/responses").respond(
        json=response(
            status="failed",
            error={"code": "server_error"},
            usage={"input_tokens": 50, "output_tokens": 10},
        )
    )
    try:
        with pytest.raises(ProviderTransientError):
            await provider.generate_structured(
                [], OutputSpec("sample", {"type": "object", "properties": {}}, dict)
            )
    finally:
        await provider.aclose()
    assert sink.write.call_args.args[0]["input"] == 50
    assert sink.write.call_args.args[0]["output"] == 10
    assert sink.write.call_args.args[0]["error"] == "ProviderTransientError"


async def test_function_call_uses_call_id_and_replays_native_pair(respx_mock):
    provider = OpenAIChatProvider(model="gpt-6.1-sol")
    raw = response(
        output=[
            {
                "type": "function_call",
                "id": "item-1",
                "call_id": "call-1",
                "name": "peek",
                "arguments": "{}",
                "status": "completed",
            }
        ]
    )
    respx_mock.post("https://api.openai.com/v1/responses").respond(json=raw)
    try:
        reply = await provider.chat(
            [UserMessage("look")],
            [{"name": "peek", "input_schema": {"type": "object", "properties": {}}}],
            thinking="high",
        )
        wire = provider.serialize_history(
            [reply, ToolResultMessage(reply.tool_calls[0].id, "observed")]
        )
    finally:
        await provider.aclose()
    assert reply.finish_reason == FinishReason.TOOL_CALLS
    assert wire[0] == raw["output"][0]
    assert wire[1]["call_id"] == "call-1"
    assert wire[1]["type"] == "function_call_output"


@pytest.mark.parametrize(
    "arguments", ['{"x":1,"x":2}', '{"x":NaN}', "[]", "incomplete"]
)
async def test_bad_function_arguments_fail_before_any_dispatch(arguments, respx_mock):
    from physiclaw.common.provider_errors import ProviderTransientError

    provider = OpenAIChatProvider(model="gpt-6.1-sol")
    respx_mock.post("https://api.openai.com/v1/responses").respond(
        json=response(
            output=[
                {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "peek",
                    "arguments": arguments,
                },
                {
                    "type": "function_call",
                    "call_id": "call-2",
                    "name": "peek",
                    "arguments": "{}",
                },
            ]
        )
    )
    try:
        with pytest.raises(ProviderTransientError):
            await provider.chat([], [])
    finally:
        await provider.aclose()


async def test_refusal_after_malformed_content_still_stops_generation(respx_mock):
    from physiclaw.contract.structured import OutputSpec

    provider = OpenAIChatProvider(model="gpt-6.1-sol")
    raw = response()
    raw["output"][0]["content"] = [None, {"type": "refusal", "refusal": "blocked"}]
    respx_mock.post("https://api.openai.com/v1/responses").respond(json=raw)
    try:
        result = await provider.generate_structured(
            [], OutputSpec("sample", {"type": "object", "properties": {}}, dict)
        )
    finally:
        await provider.aclose()
    assert result.value is None
    assert result.failure.kind == "refusal"
    assert result.reply.decode_error
