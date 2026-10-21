"""Liquid/OpenRouter transport boundaries; no real credentials or network."""

import asyncio
import base64
import json
import traceback
from dataclasses import replace

import httpx
import pytest

from physiclaw.common import config
from physiclaw.common.model_ref import parse_model_ref
from physiclaw.common.provider_errors import (
    ProviderConfigurationError,
    ProviderPermanentError,
    ProviderRequestError,
    ProviderTransientError,
)
from physiclaw.contract.decision import DecisionContext, DecisionElement
from physiclaw.provider import (
    make_chat_provider,
    make_decision_provider,
    provider_capabilities,
    provider_key_status,
)

ROUTES = [
    ("liquid", "d1"),
    ("liquid", "d1-ultra"),
    ("openrouter", "liquid/d1-ultra"),
    ("openrouter", "typesafe/jev-1.14"),
]
SECRET = "fixture-only-credential"


@pytest.fixture(params=ROUTES)
async def provider(request, monkeypatch):
    vendor, model = request.param
    monkeypatch.setenv(f"{vendor.upper()}_API_KEY", SECRET)
    instance = make_decision_provider(vendor, model, require_images=True)
    yield instance
    await instance.aclose()


def deadline():
    return asyncio.get_running_loop().time() + 5


def answer():
    return {
        "id": "native-request-17",
        "model": "reported-model-version",
        "answers": {
            "next_action": {
                "type": "choice",
                "choice": "a1",
                "confidence": 0.81,
                "probabilities": {"YIELD": 0.1, "a1": 0.89},
            },
        },
        # Usage is already aggregated across questions; never multiply it.
        "usage": {"input_tokens": 900, "output_tokens": 0},
    }


async def test_json_transport_aliases_and_image_integrity(
    provider, image_decision_request, respx_mock
):
    full_id = "1234567890abcdef1234567890abcdef"
    context = image_decision_request.context
    frame = replace(context.frame, id=full_id)
    context = DecisionContext(
        full_id,
        facts={"literal": full_id, "recent_actions": [{"observation_id": full_id}]},
        frame=frame,
        ocr=(DecisionElement(full_id + ":0", context.ocr[0].bbox, "微信"),),
        icons=(DecisionElement(full_id + ":1", context.icons[0].bbox),),
    )
    question = image_decision_request.questions[0]
    request = replace(
        image_decision_request,
        context=context,
        questions=(
            replace(
                question,
                candidates=(
                    question.candidates[0],
                    replace(question.candidates[1], target_ref=full_id + ":0"),
                ),
            ),
        ),
    )
    route = respx_mock.post(str(provider._client.base_url) + provider.ENDPOINT).respond(
        json=answer()
    )
    result = await provider.evaluate(request, deadline=deadline())
    result.validate_for(request)
    assert result.provider_id == provider.PROVIDER_ID
    assert result.requested_model_id == provider.model
    assert result.model_id == "reported-model-version"
    assert result.provider_request_id == "native-request-17"
    assert result.usage.input_tokens == 900
    assert result.usage.output_tokens == 0
    assert result.answers["next_action"].confidence == 0.81
    assert result.answers["next_action"].probabilities["a1"] == pytest.approx(
        0.89 / 0.99
    )
    assert route.call_count == 1
    sent = route.calls[0].request
    assert sent.headers["authorization"] == f"Bearer {SECRET}"
    assert sent.headers["content-type"] == "application/json"
    assert "typesafe-danger-zone-images" not in sent.headers
    body = json.loads(sent.content)
    if provider.PROVIDER_ID == "liquid":
        state = body["state"]
        image_url = body["images"][0]
    else:
        state = json.loads(body["state"][0])
        image_url = body["state"][1]["image_url"]["url"]
        assert "images" not in body
    assert base64.b64decode(image_url.split(",", 1)[1]) == frame.data
    assert state["current_image"]["sha256"] == frame.sha256
    assert state["observation"]["id"] == full_id[:7]
    assert state["observation"]["ocr"][0]["ref"] == full_id[:7] + ":0"
    assert state["facts"]["recent_actions"][0]["observation_id"] == full_id[:7]
    assert state["facts"]["literal"] == full_id
    assert (
        body["questions"]["next_action"]["criteria"]["a1"]["target_ref"]
        == full_id[:7] + ":0"
    )
    assert request.context.observation_id == full_id
    assert request.questions[0].candidates[1].target_ref == full_id + ":0"
    assert "assets" not in body and "messages" not in body
    assert SECRET not in sent.content.decode()


async def test_text_request_omits_image_fields(provider, decision_request, respx_mock):
    route = respx_mock.post(str(provider._client.base_url) + provider.ENDPOINT).respond(
        json=answer()
    )
    await provider.evaluate(decision_request, deadline=deadline())
    body = json.loads(route.calls[0].request.content)
    assert set(body) == {"model", "state", "questions"}
    assert isinstance(body["state"], dict)
    assert "current_image" not in body["state"]


@pytest.mark.parametrize("size", [(3201, 3200), (3201, 32)])
async def test_image_limits_reject_before_http(
    provider, image_decision_request, respx_mock, size
):
    frame = replace(image_decision_request.context.frame, width=size[0], height=size[1])
    request = replace(
        image_decision_request,
        context=replace(image_decision_request.context, frame=frame),
    )
    with pytest.raises(ProviderRequestError, match="image"):
        await provider.evaluate(request, deadline=deadline())
    assert not respx_mock.calls


async def test_other_openrouter_models_do_not_inherit_jev_image_limits(
    monkeypatch, image_decision_request, respx_mock
):
    monkeypatch.setenv("OPENROUTER_API_KEY", SECRET)
    provider = make_decision_provider(
        "openrouter", "vendor/future", require_images=True
    )
    frame = replace(image_decision_request.context.frame, width=2000)
    request = replace(
        image_decision_request,
        context=replace(image_decision_request.context, frame=frame),
    )
    route = respx_mock.post("https://openrouter.ai/api/alpha/decisions").respond(
        json=answer()
    )
    try:
        await provider.evaluate(request, deadline=deadline())
        state = json.loads(json.loads(route.calls[0].request.content)["state"][0])
        assert state["current_image"]["width"] == 2000
    finally:
        await provider.aclose()


async def test_encoded_json_size_is_bounded(provider, decision_request, respx_mock):
    request = replace(
        decision_request,
        context=replace(
            decision_request.context, facts={"large": "x" * provider._request_limit()}
        ),
    )
    with pytest.raises(ProviderRequestError, match="byte limit"):
        await provider.evaluate(request, deadline=deadline())
    assert not respx_mock.calls


@pytest.mark.parametrize(
    "status,kind",
    [
        (401, ProviderPermanentError),
        (403, ProviderPermanentError),
        (413, ProviderRequestError),
        (422, ProviderRequestError),
        (429, ProviderTransientError),
        (503, ProviderTransientError),
    ],
)
async def test_errors_do_not_retry_or_expose_response(
    provider, decision_request, respx_mock, status, kind
):
    route = respx_mock.post(str(provider._client.base_url) + provider.ENDPOINT).respond(
        status, text=SECRET
    )
    with pytest.raises(kind) as error:
        await provider.evaluate(decision_request, deadline=deadline())
    assert SECRET not in "".join(traceback.format_exception(error.value))
    assert route.call_count == 1


async def test_transport_errors_are_sanitized(provider, decision_request, respx_mock):
    respx_mock.post(str(provider._client.base_url) + provider.ENDPOINT).mock(
        side_effect=httpx.ConnectError(SECRET)
    )
    with pytest.raises(ProviderTransientError) as error:
        await provider.evaluate(decision_request, deadline=deadline())
    assert SECRET not in "".join(traceback.format_exception(error.value))


async def test_cancellation_and_deadline_do_not_retry(
    provider, decision_request, respx_mock
):
    entered = asyncio.Event()
    attempts = []

    async def block(request):
        attempts.append(request)
        entered.set()
        await asyncio.Event().wait()

    route = respx_mock.post(str(provider._client.base_url) + provider.ENDPOINT).mock(
        side_effect=block
    )
    task = asyncio.create_task(provider.evaluate(decision_request, deadline=deadline()))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProviderTransientError, match="timeout"):
        await provider.evaluate(
            decision_request, deadline=asyncio.get_running_loop().time() + 0.05
        )
    assert len(attempts) == 2
    route.respond(json=answer())
    assert (
        await provider.evaluate(decision_request, deadline=deadline())
    ).request_id == decision_request.id


@pytest.mark.parametrize("vendor,model", ROUTES)
def test_credentials_are_vendor_scoped_and_services_stay_separate(
    vendor, model, monkeypatch, tmp_path
):
    monkeypatch.setattr(config, "CONFIG", config.Config())
    for name in ("LIQUID_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TYPESAFE_API_KEY", "different-vendor-key")
    assert provider_key_status(vendor) == (None, None)
    with pytest.raises(ProviderConfigurationError, match=f"{vendor.upper()}_API_KEY"):
        make_decision_provider(vendor, model)
    path = tmp_path / "config.toml"
    path.write_text(f'[provider]\n{vendor}_api_key = "{SECRET}"\n')
    monkeypatch.setattr(config, "CONFIG", config.load(path))
    assert provider_key_status(vendor) == (
        "********",
        f"config.toml [provider] {vendor}_api_key",
    )
    monkeypatch.setenv(f"{vendor.upper()}_API_KEY", "env-override")
    assert provider_key_status(vendor) == ("********", f"{vendor.upper()}_API_KEY env")
    if vendor == "liquid":
        assert provider_capabilities(vendor) == {"decision"}
        with pytest.raises(ValueError, match="unknown provider"):
            make_chat_provider(vendor, model)
    else:
        assert provider_capabilities(vendor) == {"chat", "decision"}
    assert parse_model_ref(f"{vendor}/{model}") == (vendor, model)


@pytest.mark.parametrize("vendor,model", ROUTES)
async def test_custom_endpoint_and_config_credentials(
    vendor, model, monkeypatch, decision_request, respx_mock
):
    monkeypatch.delenv(f"{vendor.upper()}_API_KEY", raising=False)
    monkeypatch.setattr(config, "CONFIG", config.Config())
    setattr(config.CONFIG.provider, f"{vendor}_api_key", SECRET)
    path = config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'[providers.{vendor}]\nbase_url = "https://proxy.example/custom/v1"\n'
    )
    provider = make_decision_provider(vendor, model)
    try:
        route = respx_mock.post(
            "https://proxy.example/custom/v1/" + provider.ENDPOINT
        ).respond(json=answer())
        await provider.evaluate(decision_request, deadline=deadline())
        assert route.calls[0].request.headers["authorization"] == f"Bearer {SECRET}"
    finally:
        await provider.aclose()
