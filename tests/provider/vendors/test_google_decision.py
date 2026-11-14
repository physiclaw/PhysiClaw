"""Native Gemini Decisions wire contract and separation from Google chat."""

import base64
import json
import traceback
from dataclasses import replace

import httpx
import pytest

from physiclaw.common import config
from physiclaw.common.provider_errors import (
    ProviderPermanentError,
    ProviderRequestError,
    ProviderTransientError,
)
from physiclaw.contract.decision import DecisionCapabilities
from physiclaw.contract.dto import UserMessage
from physiclaw.provider import (
    decision_provider_capabilities,
    make_chat_provider,
    make_decision_provider,
    provider_capabilities,
)
from physiclaw.provider.decision import base as decision_base
from physiclaw.provider.vendors.google.chat import GoogleChatProvider
from physiclaw.provider.vendors.google.decision import GoogleDecisionProvider

URL = "https://generativelanguage.googleapis.com/v1beta/decisions"
CHAT_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
SECRET = "google-fixture-not-a-real-key"


def deadline():
    return decision_base.asyncio.get_running_loop().time() + 5


def answer(choice="a1"):
    # Live Gemini responses contain answers and usage, without a model field.
    return {
        "answers": {
            "next_action": {
                "type": "choice",
                "choice": choice,
                "confidence": 0.81,
                "probabilities": {
                    "a1": 0.9 if choice == "a1" else 0.1,
                    "YIELD": 0.1 if choice == "a1" else 0.9,
                },
            }
        },
        "usage": {
            "total_input_tokens": 300,
            "total_output_tokens": 0,
            "total_tokens": 300,
            "input_tokens_by_modality": [{"modality": "text", "tokens": 300}],
        },
    }


@pytest.fixture(autouse=True)
def google_key(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", SECRET)


@pytest.fixture
async def provider():
    instance = make_decision_provider(
        "google", "gemini-decision-1", require_images=True
    )
    yield instance
    await instance.aclose()


def test_registered_capabilities_and_shared_credentials():
    assert provider_capabilities("google") == {"chat", "decision"}
    assert GoogleDecisionProvider.credentials() == GoogleChatProvider.credentials()
    assert decision_provider_capabilities("google", "gemini-decision-1") == (
        DecisionCapabilities(
            images=True,
            probabilities=True,
            confidence=True,
            max_choices=255,
            max_questions=1000,
        )
    )


@pytest.mark.parametrize("choice", ["a1", "YIELD"])
async def test_native_request_and_reply(provider, decision_request, respx_mock, choice):
    route = respx_mock.post(URL).respond(
        json=answer(choice), headers={"x-request-id": "google-r1"}
    )
    result = await provider.evaluate(decision_request, deadline=deadline())
    assert result.answers["next_action"].selected_id == choice
    assert result.answers["next_action"].confidence == 0.81
    assert result.usage.input_tokens == 300
    assert result.usage.output_tokens == 0
    assert result.usage.cached_tokens is result.usage.cache_creation_tokens is None
    assert result.model_id == result.requested_model_id == "gemini-decision-1"
    assert result.provider_request_id == "google-r1"
    sent = route.calls[0].request
    body = json.loads(sent.content)
    assert set(body) == {"model", "input", "questions"}
    assert body["input"][0]["type"] == "text"
    state = json.loads(body["input"][0]["text"])
    assert state["facts"]["resolved_inputs"] == {"app_name": "微信"}
    assert state["observation"]["ocr"][0]["ref"] == "o17:0"
    question = body["questions"]["next_action"]
    assert question["type"] == "choice"
    assert question["instructions"] == decision_request.questions[0].instructions
    assert json.loads(question["criteria"]["a1"]) == {
        "description": "Tap the observed WeChat text",
        "tool": "tap",
        "arguments": {"bbox": [0.1, 0.6, 0.3, 0.8]},
        "target_ref": "o17:0",
    }
    assert sent.headers["x-goog-api-key"] == SECRET
    assert "authorization" not in sent.headers
    assert SECRET not in str(sent.url) and SECRET.encode() not in sent.content
    assert route.call_count == 1


async def test_inline_image_preserves_observation(
    provider, image_decision_request, respx_mock
):
    route = respx_mock.post(URL).respond(json=answer())
    await provider.evaluate(image_decision_request, deadline=deadline())
    body = json.loads(route.calls[0].request.content)
    text, image = body["input"]
    frame = image_decision_request.context.frame
    assert image["type"] == "image"
    assert image["mime_type"] == frame.media_type
    assert base64.b64decode(image["data"]) == frame.data
    state = json.loads(text["text"])
    assert state["observation"]["id"] == state["current_image"]["id"] == frame.id
    assert state["current_image"]["sha256"] == frame.sha256


async def test_chat_and_decision_use_separate_endpoints(
    provider, decision_request, respx_mock
):
    decision_route = respx_mock.post(URL).respond(json=answer())
    chat_route = respx_mock.post(CHAT_URL).respond(
        json={
            "choices": [
                {
                    "message": {"role": "assistant", "content": "Ready"},
                    "finish_reason": "stop",
                }
            ]
        }
    )
    chat = make_chat_provider("google", "gemini-3-flash-preview")
    try:
        assert (
            await chat.chat([UserMessage("Read the request")], [])
        ).content == "Ready"
        await provider.evaluate(decision_request, deadline=deadline())
        assert chat._client is not provider._client
        assert chat_route.call_count == decision_route.call_count == 1
        assert "questions" not in json.loads(chat_route.calls[0].request.content)
        assert "messages" not in json.loads(decision_route.calls[0].request.content)
    finally:
        await chat.aclose()


@pytest.mark.parametrize(
    "patch",
    [
        {"choice": True},
        {"choice": "invented"},
        {"type": "noul"},
        {"confidence": True},
        {"confidence": 1.1},
        {"probabilities": [["a1", 0.9], ["YIELD", 0.1]]},
        {"probabilities": {"a1": 0.9}},
        {"probabilities": {"a1": 0.9, "YIELD": 0.2}},
        {"probabilities": {"a1": True, "YIELD": 0}},
        {"probabilities": {"a1": 0.1, "YIELD": 0.9}},
    ],
)
async def test_malformed_answers_are_rejected(
    provider, decision_request, respx_mock, patch
):
    payload = answer()
    payload["answers"]["next_action"].update(patch)
    respx_mock.post(URL).respond(json=payload)
    with pytest.raises(ProviderTransientError, match="malformed"):
        await provider.evaluate(decision_request, deadline=deadline())


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"answers": []},
        {"answers": {"next_action": []}},
        {"choices": [{"message": {"content": "a1"}}]},
    ],
)
async def test_malformed_envelopes(provider, decision_request, respx_mock, payload):
    route = respx_mock.post(URL).respond(json=payload)
    with pytest.raises(ProviderTransientError, match="malformed"):
        await provider.evaluate(decision_request, deadline=deadline())
    assert route.call_count == 1


@pytest.mark.parametrize(
    "usage,expected",
    [
        (None, (None, None)),
        ([], (None, None)),
        ({}, (None, None)),
        ({"total_input_tokens": True, "total_output_tokens": -1}, (None, None)),
        ({"total_input_tokens": 321, "total_output_tokens": 0}, (321, 0)),
    ],
)
async def test_usage_is_reported_or_unknown(
    provider, decision_request, respx_mock, usage, expected
):
    payload = answer()
    payload["usage"] = usage
    respx_mock.post(URL).respond(json=payload)
    result = await provider.evaluate(decision_request, deadline=deadline())
    assert (result.usage.input_tokens, result.usage.output_tokens) == expected


@pytest.mark.parametrize(
    "status,kind",
    [
        (400, ProviderRequestError),
        (401, ProviderPermanentError),
        (403, ProviderPermanentError),
        (404, ProviderPermanentError),
        (429, ProviderTransientError),
        (500, ProviderTransientError),
    ],
)
async def test_http_errors_are_redacted_and_not_retried(
    provider, decision_request, respx_mock, status, kind
):
    route = respx_mock.post(URL).respond(status, json={"error": {"message": SECRET}})
    with pytest.raises(kind) as error:
        await provider.evaluate(decision_request, deadline=deadline())
    assert SECRET not in "".join(traceback.format_exception(error.value))
    assert route.call_count == 1


async def test_transport_failure_is_redacted(provider, decision_request, respx_mock):
    route = respx_mock.post(URL).mock(side_effect=httpx.ReadTimeout(SECRET))
    with pytest.raises(ProviderTransientError) as error:
        await provider.evaluate(decision_request, deadline=deadline())
    assert SECRET not in "".join(traceback.format_exception(error.value))
    assert route.call_count == 1


async def test_question_limit_precedes_http(provider, decision_request, respx_mock):
    request = replace(
        decision_request,
        questions=tuple(
            replace(decision_request.questions[0], id=f"q{i}") for i in range(1001)
        ),
    )
    with pytest.raises(ProviderRequestError, match="at most 1000 questions"):
        await provider.evaluate(request, deadline=deadline())
    assert not respx_mock.calls


async def test_config_key_and_custom_native_endpoint(
    monkeypatch, decision_request, respx_mock
):
    monkeypatch.delenv("GOOGLE_API_KEY")
    monkeypatch.setattr(config, "CONFIG", config.Config())
    monkeypatch.setattr(config.CONFIG.provider, "google_api_key", SECRET)
    monkeypatch.setattr(
        config,
        "provider_base_url_override",
        lambda _: "https://google.example.test/v1beta",
    )
    instance = make_decision_provider("google", "gemini-decision-1")
    route = respx_mock.post("https://google.example.test/v1beta/decisions").respond(
        json=answer()
    )
    try:
        await instance.evaluate(decision_request, deadline=deadline())
        assert route.calls[0].request.headers["x-goog-api-key"] == SECRET
    finally:
        await instance.aclose()
