"""Native OpenAI Decisions wire and strict separation from chat services."""

import asyncio
import base64
import json
import traceback
from dataclasses import replace

import httpx
import pytest

from physiclaw.contract.dto import UserMessage
from physiclaw.provider import (
    ProviderPermanentError,
    ProviderRequestError,
    ProviderTransientError,
    make_chat_provider,
    make_decision_provider,
)

URL = "https://api.openai.com/v1/decisions"
CHAT_URL = "https://api.openai.com/v1/chat/completions"
SECRET = "openai-fixture-not-a-real-key"


def deadline():
    return asyncio.get_running_loop().time() + 5


def answer(choice="a1"):
    return {
        "model": "gpt-6-luna",
        "answers": [
            {
                "type": "choice",
                "name": "next_action",
                "choice": choice,
                "confidence": 0.81,
                "probabilities": [
                    {"value": "YIELD", "probability": 0.1 if choice == "a1" else 0.9},
                    {"value": "a1", "probability": 0.9 if choice == "a1" else 0.1},
                ],
            }
        ],
        "usage": {"input_tokens": 300, "output_tokens": 0},
    }


@pytest.fixture(autouse=True)
def openai_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)


@pytest.fixture
async def provider():
    instance = make_decision_provider("openai", "gpt-6-luna", require_images=True)
    yield instance
    await instance.aclose()


@pytest.mark.parametrize("choice", ["a1", "YIELD"])
async def test_choice_request_and_normalized_reply(
    provider, decision_request, respx_mock, choice
):
    route = respx_mock.post(URL).respond(
        json=answer(choice), headers={"x-request-id": "openai-native-r1"}
    )
    result = await provider.evaluate(decision_request, deadline=deadline())
    assert result.answers["next_action"].selected_id == choice
    assert result.provider_id == "openai"
    assert result.requested_model_id == result.model_id == "gpt-6-luna"
    assert result.answers["next_action"].confidence == 0.81
    assert set(result.answers["next_action"].probabilities) == {"YIELD", "a1"}
    assert result.usage.input_tokens == 300
    assert result.usage.output_tokens == 0
    assert result.provider_request_id == "openai-native-r1"
    assert result.elapsed_seconds >= 0
    body = json.loads(route.calls[0].request.content)
    assert set(body) == {"model", "input", "questions"}
    state = json.loads(body["input"][0]["content"][0]["text"])
    assert state["facts"]["resolved_inputs"] == {"app_name": "微信"}
    assert state["observation"]["ocr"][0]["ref"] == "o17:0"
    assert state["observation"]["icons"][0]["text"] is None
    (question,) = body["questions"]
    assert question["type"] == "choice"
    assert question["instructions"] == decision_request.questions[0].instructions
    assert [c["value"] for c in question["choices"]] == ["YIELD", "a1"]
    candidate = json.loads(question["choices"][1]["description"])
    assert candidate["tool"] == "tap"
    assert candidate["arguments"] == {"bbox": [0.1, 0.6, 0.3, 0.8]}
    assert candidate["target_ref"] == "o17:0"
    request = route.calls[0].request
    assert request.headers["authorization"] == f"Bearer {SECRET}"
    assert "typesafe-danger-zone-images" not in request.headers
    assert SECRET not in request.content.decode()
    assert route.call_count == 1


async def test_image_is_inline_and_uses_same_observation(
    provider, image_decision_request, respx_mock
):
    route = respx_mock.post(URL).respond(json=answer())
    await provider.evaluate(image_decision_request, deadline=deadline())
    body = json.loads(route.calls[0].request.content)
    text, image = body["input"][0]["content"]
    frame = image_decision_request.context.frame
    assert image["type"] == "input_image"
    prefix, encoded = image["image_url"].split(",", 1)
    assert prefix == "data:image/jpeg;base64"
    assert base64.b64decode(encoded) == frame.data
    state = json.loads(text["text"])
    assert state["observation"]["id"] == state["current_image"]["id"] == frame.id
    assert state["current_image"]["sha256"] == frame.sha256


async def test_same_model_chat_and_decision_call_different_endpoints(
    provider, decision_request, respx_mock
):
    decision_route = respx_mock.post(URL).respond(json=answer())
    chat_route = respx_mock.post(CHAT_URL).respond(
        json={
            "id": "chat-r1",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Interpret the purchase request",
                    },
                    "finish_reason": "stop",
                }
            ],
        }
    )
    chat = make_chat_provider("openai", "gpt-6-luna")
    try:
        chat_reply = await chat.chat([UserMessage(content="Read this request")], [])
        decision_reply = await provider.evaluate(decision_request, deadline=deadline())
        assert chat_reply.content == "Interpret the purchase request"
        assert decision_reply.answers["next_action"].selected_id == "a1"
        assert "questions" not in json.loads(chat_route.calls[0].request.content)
        assert "messages" not in json.loads(decision_route.calls[0].request.content)
        assert chat_route.call_count == decision_route.call_count == 1
    finally:
        await chat.aclose()


async def test_chat_response_is_rejected_by_decision_parser(
    provider, decision_request, respx_mock
):
    route = respx_mock.post(URL).respond(
        json={
            "model": "gpt-6-luna",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "a1"},
                    "finish_reason": "stop",
                }
            ],
        }
    )
    with pytest.raises(ProviderTransientError, match="malformed"):
        await provider.evaluate(decision_request, deadline=deadline())
    assert route.call_count == 1
    assert all(call.request.url.path == "/v1/decisions" for call in respx_mock.calls)


async def test_decision_response_is_rejected_by_chat_parser(respx_mock):
    route = respx_mock.post(CHAT_URL).respond(json=answer())
    chat = make_chat_provider("openai", "gpt-6-luna")
    try:
        with pytest.raises(ProviderTransientError):
            await chat.chat([UserMessage(content="Read request")], [])
        assert route.call_count == 1
        assert all(
            call.request.url.path == "/v1/chat/completions" for call in respx_mock.calls
        )
    finally:
        await chat.aclose()


@pytest.mark.parametrize(
    "patch",
    [
        {"choice": True},
        {"choice": "invented"},
        {"name": "other-question"},
        {"type": "score"},
        {
            "probabilities": [
                {"value": "a1", "probability": 0.6},
                {"value": "a1", "probability": 0.4},
            ]
        },
        {"probabilities": [{"value": True, "probability": 1}]},
        {"probabilities": [{"value": "a1", "probability": 0.5}]},
        {"probabilities": {"a1": 1}},
        {"confidence": True},
    ],
)
async def test_invalid_choice_responses(provider, decision_request, respx_mock, patch):
    payload = answer()
    payload["answers"][0].update(patch)
    respx_mock.post(URL).respond(json=payload)
    with pytest.raises(ProviderTransientError, match="malformed"):
        await provider.evaluate(decision_request, deadline=deadline())


async def test_refusal_rejects_request_without_disabling_provider(
    provider, decision_request, respx_mock
):
    route = respx_mock.post(URL).respond(
        json={
            "model": "gpt-6-luna",
            "answers": [{"type": "refusal", "name": "next_action"}],
        }
    )
    with pytest.raises(ProviderRequestError, match="refused"):
        await provider.evaluate(decision_request, deadline=deadline())
    assert route.call_count == 1
    assert len(respx_mock.calls) == 1
    route.respond(json=answer())
    result = await provider.evaluate(decision_request, deadline=deadline())
    assert result.request_id == decision_request.id
    assert all(call.request.url.path == "/v1/decisions" for call in respx_mock.calls)


@pytest.mark.parametrize(
    "status,kind",
    [
        (401, ProviderPermanentError),
        (429, ProviderTransientError),
        (503, ProviderTransientError),
    ],
)
async def test_native_http_failures_are_sanitized_without_retry(
    provider, decision_request, respx_mock, status, kind
):
    route = respx_mock.post(URL).respond(status, text=SECRET)
    with pytest.raises(kind, match=f"HTTP {status}") as error:
        await provider.evaluate(decision_request, deadline=deadline())
    assert SECRET not in "".join(traceback.format_exception(error.value))
    assert route.call_count == 1


async def test_transport_failures_do_not_invoke_chat(
    provider, decision_request, respx_mock
):
    route = respx_mock.post(URL).mock(side_effect=httpx.ReadTimeout(SECRET))
    with pytest.raises(ProviderTransientError) as error:
        await provider.evaluate(decision_request, deadline=deadline())
    assert SECRET not in "".join(traceback.format_exception(error.value))
    assert route.call_count == 1


async def test_missing_accounting_and_scores_stay_unknown(
    provider, decision_request, respx_mock
):
    payload = answer()
    payload.pop("usage")
    payload["answers"][0].pop("probabilities")
    payload["answers"][0].pop("confidence")
    respx_mock.post(URL).respond(json=payload)
    result = await provider.evaluate(decision_request, deadline=deadline())
    assert result.usage.input_tokens is result.usage.output_tokens is None
    assert (
        result.answers["next_action"].confidence
        is result.answers["next_action"].probabilities
        is None
    )


async def test_question_limit_precedes_http(provider, decision_request, respx_mock):
    request = replace(
        decision_request,
        questions=tuple(
            replace(decision_request.questions[0], id=f"q{i}") for i in range(201)
        ),
    )
    with pytest.raises(ProviderRequestError, match="200 questions"):
        await provider.evaluate(request, deadline=deadline())
    assert not respx_mock.calls
