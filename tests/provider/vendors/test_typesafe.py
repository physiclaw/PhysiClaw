"""Native TypeSafe wire/transport tests. No key or network access required."""

import asyncio
import json
import traceback
from dataclasses import replace
from email import policy
from email.parser import BytesParser
from math import inf, nan

import httpx
import pytest

from physiclaw.common import config
from physiclaw.common.config import config_path
from physiclaw.contract.decision import ActionCandidate
from physiclaw.provider import (
    ProviderConfigurationError,
    ProviderPermanentError,
    ProviderTransientError,
    make_decision_provider,
)
from physiclaw.provider.vendors.typesafe.decision import TypeSafeDecisionProvider

URL = "https://api.typesafe.ai/v1/systemone"
SECRET = "fixture-secret-not-a-real-key"


def deadline(seconds=5):
    return asyncio.get_running_loop().time() + seconds


def answer(choice="a1", *, model="jev-1.14.0"):
    return {
        "model": model,
        "answers": {
            "next_action": {
                "type": "choice",
                "choice": choice,
                "confidence": 0.81,
                "probabilities": {
                    "YIELD": 0.1 if choice == "a1" else 0.9,
                    "a1": 0.9 if choice == "a1" else 0.1,
                },
            }
        },
        "usage": {"input_tokens": 300, "output_tokens": 50},
    }


@pytest.fixture(autouse=True)
def typesafe_key(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", SECRET)


@pytest.fixture
async def provider():
    instance = make_decision_provider("typesafe", "jev-1.14.0", require_images=True)
    yield instance
    await instance.aclose()


@pytest.mark.parametrize("choice", ["a1", "YIELD"])
async def test_native_choice_and_yield(provider, decision_request, respx_mock, choice):
    route = respx_mock.post(URL).respond(
        json=answer(choice), headers={"x-request-id": "native-r1"}
    )
    result = await provider.evaluate(decision_request, deadline=deadline())
    assert result.selected_id == choice
    assert result.request_id == "r17"
    assert result.provider_id == "typesafe"
    assert result.model_id == result.requested_model_id == "jev-1.14.0"
    assert result.confidence == 0.81
    assert (
        dict(result.probabilities)
        == answer(choice)["answers"]["next_action"]["probabilities"]
    )
    assert result.usage.input_tokens == 300
    assert result.usage.output_tokens == 50
    assert result.elapsed_seconds >= 0
    assert result.provider_request_id == "native-r1"
    request = route.calls[0].request
    assert request.headers["authorization"] == f"Bearer {SECRET}"
    assert request.headers["content-type"] == "application/json"
    assert "typesafe-danger-zone-images" not in request.headers
    body = json.loads(request.content)
    assert set(body) == {"model", "state", "questions"}
    assert body["state"]["facts"]["resolved_inputs"] == {"app_name": "微信"}
    observation = body["state"]["observation"]
    assert observation["id"] == "o17"
    assert observation["ocr"][0]["text"] == "微信"
    assert observation["icons"][0] == {
        "ref": "o17:e2",
        "bbox": [0.1, 0.3, 0.3, 0.5],
        "text": None,
    }
    question = body["questions"]["next_action"]
    assert question["type"] == "choice"
    assert question["instructions"] == decision_request.instructions
    assert list(question["criteria"]) == ["YIELD", "a1"]
    assert question["criteria"]["a1"] == {
        "description": "Tap the observed WeChat text",
        "tool": "tap",
        "arguments": {"bbox": [0.1, 0.6, 0.3, 0.8]},
        "target_ref": "o17:e1",
    }
    assert SECRET not in request.content.decode()
    assert route.call_count == 1


async def test_complete_image_ocr_icons_multipart(
    provider, image_decision_request, respx_mock
):
    route = respx_mock.post(URL).respond(json=answer())
    result = await provider.evaluate(image_decision_request, deadline=deadline())
    result.validate_for(image_decision_request)
    request = route.calls[0].request
    assert request.headers["TypeSafe-Danger-Zone-Images"] == "true"
    content_type = request.headers["content-type"]
    assert content_type.startswith("multipart/form-data; boundary=")
    multipart = BytesParser(policy=policy.default).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
        + request.content
    )
    parts = list(multipart.iter_parts())
    assert len(parts) == 2
    assert parts[0].get_param("name", header="content-disposition") == "request"
    assert parts[0].get_content_type() == "application/json"
    assert parts[1].get_param("name", header="content-disposition") == "current-frame"
    assert parts[1].get_content_type() == "image/jpeg"
    frame = image_decision_request.context.frame
    assert parts[1].get_payload(decode=True) == frame.data
    body = json.loads(parts[0].get_payload(decode=True))
    (image_ref,) = body["assets"]
    assert body["assets"][image_ref] == {"type": "image", "part": "current-frame"}
    assert json.dumps(body["state"]).count(image_ref) == 1
    assert json.dumps(body).count(image_ref) == 2  # definition + one reference
    assert body["state"]["current_image"] == {
        "image": image_ref,
        "width": 300,
        "height": 300,
        "sha256": frame.sha256,
    }
    assert body["state"]["observation"]["ocr"]
    assert body["state"]["observation"]["icons"]
    assert SECRET not in json.dumps(body)


async def test_png_multipart(provider, image_decision_request, respx_mock):
    import cv2
    import numpy as np

    _, png = cv2.imencode(".png", np.zeros((300, 300, 3), dtype=np.uint8))
    frame = replace(
        image_decision_request.context.frame, media_type="image/png", data=png.tobytes()
    )
    request = replace(
        image_decision_request,
        context=replace(image_decision_request.context, frame=frame),
    )
    route = respx_mock.post(URL).respond(json=answer())
    await provider.evaluate(request, deadline=deadline())
    assert b'filename="frame.png"' in route.calls[0].request.content
    assert b"Content-Type: image/png" in route.calls[0].request.content


@pytest.mark.parametrize("model", ["vendor-model-alpha", "organization/future-model"])
async def test_model_id_pass_through_and_actual_identity(
    model, decision_request, respx_mock
):
    provider = make_decision_provider("typesafe", model)
    try:
        route = respx_mock.post(URL).respond(
            json=answer(model="resolved-model-version")
        )
        result = await provider.evaluate(decision_request, deadline=deadline())
        assert json.loads(route.calls[0].request.content)["model"] == model
        assert result.requested_model_id == model
        assert result.model_id == "resolved-model-version"
    finally:
        await provider.aclose()


async def test_model_capability_does_not_silently_drop_image(
    image_decision_request, respx_mock
):
    provider = TypeSafeDecisionProvider("unverified-model")
    try:
        with pytest.raises(ProviderPermanentError, match="does not support images"):
            await provider.evaluate(image_decision_request, deadline=deadline())
        assert not respx_mock.calls
    finally:
        await provider.aclose()


@pytest.mark.parametrize(
    "status,kind",
    [
        (400, ProviderPermanentError),
        (401, ProviderPermanentError),
        (403, ProviderPermanentError),
        (429, ProviderTransientError),
        (500, ProviderTransientError),
        (503, ProviderTransientError),
    ],
)
async def test_http_error_no_retries_and_no_body_leak(
    provider, decision_request, respx_mock, status, kind
):
    route = respx_mock.post(URL).respond(status, text=f"Authorization: Bearer {SECRET}")
    with pytest.raises(kind, match=f"HTTP {status}") as error:
        await provider.evaluate(decision_request, deadline=deadline())
    assert SECRET not in "".join(traceback.format_exception(error.value))
    assert route.call_count == 1


@pytest.mark.parametrize(
    "exception", [httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError]
)
async def test_transport_error_no_retries_or_credential_leak(
    provider, decision_request, respx_mock, exception
):
    route = respx_mock.post(URL).mock(side_effect=exception(f"secret={SECRET}"))
    with pytest.raises(ProviderTransientError) as error:
        await provider.evaluate(decision_request, deadline=deadline())
    assert SECRET not in "".join(traceback.format_exception(error.value))
    assert route.call_count == 1


async def test_remaining_absolute_deadline_caps_entire_call(
    provider, decision_request, respx_mock
):
    started = asyncio.Event()
    attempts = []

    async def blocked(request):
        attempts.append(request)
        started.set()
        await asyncio.Event().wait()

    respx_mock.post(URL).mock(side_effect=blocked)
    before = asyncio.get_running_loop().time()
    with pytest.raises(ProviderTransientError, match="timeout"):
        await provider.evaluate(decision_request, deadline=before + 0.1)
    assert started.is_set()
    # respx records completed calls; a cancelled handler needs its own counter.
    assert len(attempts) == 1
    assert asyncio.get_running_loop().time() - before < 2
    request_timeout = attempts[0].extensions["timeout"]
    assert 0 < request_timeout["read"] <= 0.1


async def test_expired_deadline_makes_no_request(
    provider, decision_request, respx_mock
):
    with pytest.raises(ProviderTransientError, match="exhausted"):
        await provider.evaluate(decision_request, deadline=deadline(-1))
    assert not respx_mock.calls


@pytest.mark.parametrize("invalid", [nan, inf, True, "tomorrow"])
async def test_invalid_deadline(provider, decision_request, respx_mock, invalid):
    with pytest.raises(ProviderPermanentError, match="deadline"):
        await provider.evaluate(decision_request, deadline=invalid)
    assert not respx_mock.calls


async def test_cancellation_propagates_and_client_remains_usable(
    provider, decision_request, respx_mock
):
    started = asyncio.Event()
    attempts = []

    async def blocked(request):
        attempts.append(request)
        started.set()
        await asyncio.Event().wait()

    route = respx_mock.post(URL).mock(side_effect=blocked)
    task = asyncio.create_task(provider.evaluate(decision_request, deadline=deadline()))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(attempts) == 1
    route.respond(json=answer())
    assert (
        await provider.evaluate(decision_request, deadline=deadline())
    ).selected_id == "a1"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"model": "x", "answers": []},
        {"model": "x", "answers": {"wrong": {}}},
    ],
)
async def test_missing_or_wrong_response_structure(
    provider, decision_request, respx_mock, payload
):
    respx_mock.post(URL).respond(
        content=json.dumps(payload), content_type="application/json"
    )
    with pytest.raises(ProviderTransientError, match="malformed"):
        await provider.evaluate(decision_request, deadline=deadline())


@pytest.mark.parametrize(
    "patch",
    [
        {"type": "score"},
        {"choice": "unoffered"},
        {"choice": {"tool": "tap"}},
        {"confidence": -1},
        {"confidence": 1.1},
        {"confidence": True},
        {"confidence": "0.8"},
        {"probabilities": {"YIELD": 0.1, "unknown": 0.9}},
        {"probabilities": {"YIELD": 0.1, "a1": 0.8}},
        {"probabilities": {"YIELD": 0.9, "a1": 0.1}},
        {"probabilities": {"YIELD": -0.1, "a1": 1.1}},
        {"probabilities": {"YIELD": True, "a1": 0}},
        {"probabilities": [0.1, 0.9]},
    ],
)
async def test_malformed_choice_or_scores(
    provider, decision_request, respx_mock, patch
):
    payload = answer()
    payload["answers"]["next_action"].update(patch)
    respx_mock.post(URL).respond(json=payload)
    with pytest.raises(ProviderTransientError, match="malformed"):
        await provider.evaluate(decision_request, deadline=deadline())


@pytest.mark.parametrize(
    "raw",
    [
        "not JSON",
        '{"model":"x","model":"y","answers":{}}',
        '{"model":"x","answers":{"next_action":{"type":"choice","choice":"a1","confidence":NaN}}}',
        '{"model":"x","answers":{"next_action":{"type":"choice","choice":"a1","probabilities":{"a1":Infinity,"YIELD":0}}}}',
    ],
)
async def test_bad_json_duplicates_and_nonfinite_numbers(
    provider, decision_request, respx_mock, raw
):
    respx_mock.post(URL).respond(text=raw)
    with pytest.raises(ProviderTransientError, match="malformed"):
        await provider.evaluate(decision_request, deadline=deadline())


@pytest.mark.parametrize("model", [None, "", 14])
async def test_missing_actual_model_is_not_invented(
    provider, decision_request, respx_mock, model
):
    respx_mock.post(URL).respond(json=answer(model=model))
    with pytest.raises(ProviderTransientError, match="malformed"):
        await provider.evaluate(decision_request, deadline=deadline())


@pytest.mark.parametrize(
    "usage",
    [
        None,
        [],
        {},
        {"input_tokens": "300", "output_tokens": -1},
        {"input_tokens": True, "output_tokens": 1.5},
    ],
)
async def test_unknown_usage_does_not_discard_valid_choice(
    provider, decision_request, respx_mock, usage
):
    payload = answer()
    payload["usage"] = usage
    respx_mock.post(URL).respond(json=payload)
    result = await provider.evaluate(decision_request, deadline=deadline())
    assert result.selected_id == "a1"
    assert result.usage.input_tokens is result.usage.output_tokens is None


async def test_missing_scores_and_partial_usage_remain_unknown(
    provider, decision_request, respx_mock
):
    payload = answer()
    payload["answers"]["next_action"].pop("confidence")
    payload["answers"]["next_action"].pop("probabilities")
    payload["usage"] = {"input_tokens": 0}
    respx_mock.post(URL).respond(json=payload)
    result = await provider.evaluate(decision_request, deadline=deadline())
    assert result.confidence is result.probabilities is None
    assert result.usage.input_tokens == 0
    assert result.usage.output_tokens is None


@pytest.mark.parametrize(
    "field,value",
    [("width", 279), ("height", 1569), ("data", 16 * 1024 * 1024 + 1)],
)
async def test_unsupported_image_rejected_before_http(
    provider, image_decision_request, respx_mock, field, value
):
    frame = replace(
        image_decision_request.context.frame,
        **{field: b"x" * value if field == "data" else value},
    )
    request = replace(
        image_decision_request,
        context=replace(image_decision_request.context, frame=frame),
    )
    with pytest.raises(ProviderPermanentError, match="TypeSafe image"):
        await provider.evaluate(request, deadline=deadline())
    assert not respx_mock.calls


async def test_choice_limit_before_http(provider, decision_request, respx_mock):
    request = replace(
        decision_request,
        candidates=tuple(
            ActionCandidate(f"a{i}", "Offered control outcome") for i in range(256)
        ),
    )
    with pytest.raises(ProviderPermanentError, match="255"):
        await provider.evaluate(request, deadline=deadline())
    assert not respx_mock.calls


async def test_close_idempotent_and_no_use_after_close(
    provider, decision_request, respx_mock
):
    await provider.aclose()
    await provider.aclose()
    assert provider._client.is_closed
    with pytest.raises(ProviderPermanentError, match="closed"):
        await provider.evaluate(decision_request, deadline=deadline())
    assert not respx_mock.calls


def test_missing_key_explains_shared_configuration(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY")
    monkeypatch.setattr(config, "CONFIG", config.Config())
    with pytest.raises(ProviderConfigurationError, match="TYPESAFE_API_KEY"):
        TypeSafeDecisionProvider("jev-1.14.0")


@pytest.mark.parametrize("timeout", [0, -1, nan, inf, True])
def test_invalid_configured_timeout(timeout):
    with pytest.raises(ProviderConfigurationError, match="timeout"):
        TypeSafeDecisionProvider("jev-1.14.0", timeout=timeout)


async def test_shared_config_credentials_and_endpoint_override(
    monkeypatch, decision_request, respx_mock
):
    monkeypatch.delenv("TYPESAFE_API_KEY")
    monkeypatch.setattr(config, "CONFIG", config.Config())
    monkeypatch.setattr(config.CONFIG.provider, "typesafe_api_key", "config-fixture")
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[providers.typesafe]\nbase_url = "https://proxy.example/custom"\n')
    provider = make_decision_provider("typesafe", "jev-1.14.0")
    try:
        route = respx_mock.post("https://proxy.example/custom/systemone").respond(
            json=answer()
        )
        await provider.evaluate(decision_request, deadline=deadline())
        assert (
            route.calls[0].request.headers["authorization"] == "Bearer config-fixture"
        )
    finally:
        await provider.aclose()
