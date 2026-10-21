"""OpenRouter chat discovery and tool continuity stay in the chat service."""

import json
from copy import deepcopy

import pytest

from physiclaw.common.provider_errors import (
    ProviderPermanentError,
    ProviderTransientError,
)
from physiclaw.contract.dto import (
    AssistantMessage,
    FinishReason,
    ImageBlock,
    SystemMessage,
    ToolResultMessage,
    UserMessage,
)
from physiclaw.contract.turn import TurnMetadata
from physiclaw.provider import make_chat_provider, make_decision_provider

URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "qwen/chat-model"


@pytest.fixture
async def provider(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "fixture-only")
    instance = make_chat_provider("openrouter", MODEL)
    instance._catalog = {MODEL: {"id": MODEL}}
    yield instance
    await instance.aclose()


def reply():
    return {
        "id": "chat-17",
        "model": MODEL,
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "reasoning": "private reasoning",
                    "reasoning_details": [
                        {"type": "reasoning.encrypted", "data": "opaque-token"}
                    ],
                    "tool_calls": [
                        {
                            "id": "call17",
                            "type": "function",
                            "function": {
                                "name": "echo_probe",
                                "arguments": '{"text":"probe-42"}',
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {
            "prompt_tokens": 70,
            "completion_tokens": 12,
            "prompt_tokens_details": {"cached_tokens": 30},
            "completion_tokens_details": {"reasoning_tokens": 5},
        },
    }


async def test_live_catalog_is_filtered_to_text_chat_models(provider, respx_mock):
    models = [
        {"id": "future/chat-model", "architecture": {"output_modalities": ["text"]}},
        {
            "id": "typesafe/jev-1.14",
            "architecture": {"output_modalities": ["decisions"]},
        },
        {
            "id": "vendor/embedding",
            "architecture": {"output_modalities": ["embeddings"]},
        },
        {
            "id": "vendor/image-generator",
            "architecture": {"output_modalities": ["text", "image"]},
        },
        {"id": "vendor/unknown"},
        {"id": "vendor/null", "architecture": None},
    ]
    route = respx_mock.get("https://openrouter.ai/api/v1/models").respond(
        json={"data": models}
    )
    assert await provider.list_models() == models[:1]
    assert route.call_count == 1


@pytest.mark.parametrize(
    "model,explicit_cache",
    [
        ("anthropic/claude-haiku-5.5", True),
        ("qwen/qwen3.8-omni-flash", False),
        ("google/gemini-3.8-flash", False),
    ],
)
async def test_only_anthropic_routes_enable_automatic_caching(
    provider, model, explicit_cache
):
    instance = make_chat_provider("openrouter", model)
    try:
        history = [SystemMessage("Stable system prompt"), UserMessage("Task")]
        wire = instance.serialize_history(history)
        assert "cache_control" not in json.dumps(wire)
        assert (
            "cache_control" in instance._request_params(metadata=None)
        ) is explicit_cache
        assert history == [SystemMessage("Stable system prompt"), UserMessage("Task")]
    finally:
        await instance.aclose()


async def test_chat_prepares_catalog_once_and_uses_mandatory_reasoning_floor(
    provider, respx_mock
):
    provider._catalog = None
    catalog = respx_mock.get("https://openrouter.ai/api/v1/models").respond(
        json={
            "data": [
                {
                    "id": MODEL,
                    "architecture": {"output_modalities": ["text"]},
                    "reasoning": {
                        "mandatory": True,
                        "supported_efforts": ["high", "medium", "low"],
                    },
                }
            ]
        }
    )
    chat = respx_mock.post(URL).respond(json=reply())
    for _ in range(2):
        await provider.chat([UserMessage("Hello")], [], thinking="off")
    assert catalog.call_count == 1
    assert chat.call_count == 2
    assert all(
        json.loads(c.request.content)["reasoning"] == {"effort": "low"}
        for c in chat.calls
    )


async def test_default_thinking_skips_catalog_preparation(provider, respx_mock):
    provider._catalog = None
    chat = respx_mock.post(URL).respond(json=reply())
    await provider.chat([UserMessage("Hello")], [])
    assert provider._catalog is None
    assert "reasoning" not in json.loads(chat.calls[0].request.content)
    assert len(respx_mock.calls) == chat.call_count == 1


async def test_failed_catalog_preparation_is_not_cached(provider, respx_mock):
    provider._catalog = None
    catalog = respx_mock.get("https://openrouter.ai/api/v1/models").respond(503)
    with pytest.raises(ProviderTransientError):
        await provider.chat([UserMessage("Hello")], [], thinking="off")
    assert provider._catalog is None
    catalog.respond(
        json={
            "data": [
                {
                    "id": MODEL,
                    "architecture": {"output_modalities": ["text"]},
                    "reasoning": {"mandatory": True},
                }
            ]
        }
    )
    chat = respx_mock.post(URL).respond(json=reply())
    await provider.chat([UserMessage("Hello")], [], thinking="off")
    assert catalog.call_count == 2
    assert json.loads(chat.calls[0].request.content)["reasoning"] == {"effort": "low"}


async def test_model_missing_from_catalog_still_reaches_chat_api(provider, respx_mock):
    provider._catalog = None
    respx_mock.get("https://openrouter.ai/api/v1/models").respond(json={"data": []})
    chat = respx_mock.post(URL).respond(json=reply())
    await provider.chat([UserMessage("Hello")], [], thinking="off")
    assert json.loads(chat.calls[0].request.content)["model"] == MODEL


@pytest.mark.parametrize("thinking", ["off", "high"])
async def test_models_without_reasoning_do_not_receive_reasoning_parameters(
    provider, respx_mock, thinking
):
    provider._catalog[MODEL]["supported_parameters"] = ["tools", "tool_choice"]
    route = respx_mock.post(URL).respond(json=reply())
    await provider.chat([UserMessage("Hello")], [], thinking=thinking)
    assert "reasoning" not in json.loads(route.calls[0].request.content)


@pytest.mark.parametrize(
    "thinking,reasoning",
    [
        ("off", {"enabled": False}),
        ("low", {"effort": "low"}),
        ("medium", {"effort": "medium"}),
        ("high", {"effort": "high"}),
    ],
)
async def test_chat_uses_unified_reasoning_and_turn_metadata(
    provider, respx_mock, thinking, reasoning
):
    route = respx_mock.post(URL).respond(json=reply())
    history = [SystemMessage("System instructions"), UserMessage("Call echo_probe")]
    metadata = TurnMetadata("session17", 0, None, 10)
    result = await provider.chat(history, [], thinking=thinking, metadata=metadata)
    sent = json.loads(route.calls[0].request.content)
    assert sent["model"] == MODEL
    assert sent["reasoning"] == reasoning
    assert sent["provider"] == {"require_parameters": True}
    assert sent["session_id"] == metadata.session_id
    assert sent["messages"][0] == {"role": "system", "content": "System instructions"}
    assert sent["messages"][-1]["content"].startswith("<turn_metadata>")
    assert "cache_control" not in route.calls[0].request.content.decode()
    assert "questions" not in sent and "state" not in sent
    assert result.content == ""
    assert result.response_id == "chat-17"
    assert result.tool_calls[0].arguments == {"text": "probe-42"}
    assert result.usage.prompt_tokens == 70
    assert result.usage.cached_tokens == 30
    assert result.usage.reasoning_tokens == 5
    assert history == [
        SystemMessage("System instructions"),
        UserMessage("Call echo_probe"),
    ]


async def test_tool_continuation_preserves_opaque_reasoning(provider, respx_mock):
    raw = reply()
    respx_mock.post(URL).respond(json=raw)
    result = await provider.chat([UserMessage("Call echo_probe")], [])
    original = deepcopy(result.vendor_extra)
    history = [result, ToolResultMessage("call17", "tool-return-73")]
    wire = provider.serialize_history(history)
    native = raw["choices"][0]["message"]
    assert wire[0]["reasoning"] == native["reasoning"]
    assert wire[0]["reasoning_details"] == native["reasoning_details"]
    assert wire[0]["tool_calls"][0]["function"]["arguments"] == '{"text": "probe-42"}'
    assert wire[1] == {
        "role": "tool",
        "tool_call_id": "call17",
        "content": "tool-return-73",
    }
    wire[0]["reasoning_details"][0]["data"] = "changed-wire"
    assert result.vendor_extra == original


@pytest.mark.parametrize("writes", [0, 14000])
async def test_openrouter_cache_writes_reach_usage_accounting(provider, writes):
    raw = reply()
    raw["usage"]["prompt_tokens_details"]["cache_write_tokens"] = writes
    assert provider._parse_response(raw).usage.cache_creation_tokens == writes


async def test_reasoning_does_not_cross_models_or_providers(provider, monkeypatch):
    result = provider._parse_response(reply())
    monkeypatch.setattr(provider, "model", "another/chat-model")
    wire = provider.serialize_history([result])[0]
    assert "reasoning" not in wire and "reasoning_details" not in wire
    foreign = AssistantMessage(
        "Visible response",
        [],
        FinishReason.STOP,
        vendor_extra={"google": {"reasoning_details": [{"data": "foreign"}]}},
    )
    assert "reasoning_details" not in provider.serialize_history([foreign])[0]


async def test_chat_and_decision_clients_share_only_credentials(provider):
    decision = make_decision_provider("openrouter", "liquid/d1-ultra")
    try:
        assert provider.api_kind == "chat" and decision.api_kind == "decision"
        assert not hasattr(provider, "evaluate") and not hasattr(decision, "chat")
        assert provider._client is not decision._client
        assert (
            provider._client.headers["authorization"]
            == decision._client.headers["authorization"]
        )
        await decision.aclose()
        assert not provider._client.is_closed
    finally:
        await decision.aclose()


async def test_invalid_chat_model_does_not_switch_to_decisions(provider, respx_mock):
    route = respx_mock.post(URL).respond(
        404, json={"error": {"message": "No chat model"}}
    )
    with pytest.raises(ProviderPermanentError):
        await provider.chat([UserMessage("Hello")], [])
    assert route.call_count == len(respx_mock.calls) == 1


async def test_user_images_remain_chat_content(provider, respx_mock):
    route = respx_mock.post(URL).respond(json=reply())
    await provider.chat([UserMessage([ImageBlock("image/jpeg", "fixture-base64")])], [])
    body = json.loads(route.calls[0].request.content)
    assert body["messages"][0]["content"][0] == {
        "type": "image_url",
        "image_url": {"url": "data:image/jpeg;base64,fixture-base64"},
    }
    assert "assets" not in body and "questions" not in body


@pytest.mark.parametrize("failure", ["error_object", "error_finish"])
async def test_http_200_generation_errors_reject_partial_tools(
    provider, respx_mock, failure
):
    raw = reply()
    if failure == "error_object":
        raw["error"] = {"message": "upstream failed", "code": 503}
    else:
        raw["choices"][0]["finish_reason"] = "error"
    route = respx_mock.post(URL).respond(json=raw)
    with pytest.raises(ProviderTransientError, match="generation failed"):
        await provider.chat([UserMessage("Call echo_probe")], [])
    assert route.call_count == 1
