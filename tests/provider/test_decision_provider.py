"""A second implementation must fit without TypeSafe-shaped scores or state."""

import asyncio
import json

import pytest

from physiclaw.common import config
from physiclaw.common.provider_errors import (
    ProviderConfigurationError,
    ProviderPermanentError,
)
from physiclaw.contract.decision import (
    DecisionAnswer,
    DecisionCapabilities,
    DecisionResult,
)
from physiclaw.provider import (
    DecisionProviderSpec,
    decision_provider_capabilities,
    in_process_provider_ids,
    make_chat_provider,
    make_decision_provider,
    provider_key_status,
    register_decision_provider,
    registry,
    validate_model_ref,
)


class FakeDecisionProvider:
    api_kind = "decision"

    def __init__(self, model: str, *, timeout: float):
        self.model = model
        self.timeout = timeout
        self.capabilities = self.capabilities_for(model)
        self.closed = False

    @staticmethod
    def capabilities_for(model):
        return DecisionCapabilities(images=model == "visual")

    async def evaluate(self, request, *, deadline):
        assert deadline > asyncio.get_running_loop().time()
        return DecisionResult(
            request.id,
            {q.id: DecisionAnswer(q.candidates[1].id) for q in request.questions},
            "fake",
            self.model,
            self.model,
        )

    async def aclose(self):
        self.closed = True


@pytest.fixture
def fake_registration(monkeypatch):
    monkeypatch.setattr(registry, "_VENDORS", dict(registry._VENDORS))
    spec = DecisionProviderSpec(
        FakeDecisionProvider,
        FakeDecisionProvider.capabilities_for,
        ("FAKE_KEY",),
        "fake_key",
    )
    register_decision_provider("fake", spec)
    return spec


async def test_fake_provider_substitution(
    decision_request, fake_registration, monkeypatch
):
    monkeypatch.setattr(config.CONFIG.engine, "provider_timeout_seconds", 42)
    provider = make_decision_provider("fake", "text")
    assert provider.timeout == 42
    result = await provider.evaluate(
        decision_request, deadline=asyncio.get_running_loop().time() + 1
    )
    result.validate_for(decision_request)
    assert result.answers["next_action"].selected_id == "a1"
    assert result.provider_id == "fake"
    answer = result.answers["next_action"]
    assert answer.confidence is answer.probabilities is None
    assert result.usage.input_tokens is None
    await provider.aclose()
    assert provider.closed


def test_capabilities_per_model_without_constructing_client(
    fake_registration, monkeypatch
):
    def forbidden(*args, **kwargs):
        pytest.fail("capability lookup must not construct a client")

    monkeypatch.setattr("httpx.AsyncClient", forbidden)
    assert decision_provider_capabilities("fake", "text").images is False
    assert decision_provider_capabilities("fake", "visual").images is True
    assert decision_provider_capabilities("typesafe", "jev-1.14.0").images is True
    assert (
        decision_provider_capabilities("typesafe", "unverified-version").images is True
    )
    assert decision_provider_capabilities("unknown", "x") is None
    with pytest.raises(ProviderConfigurationError, match="does not support images"):
        make_decision_provider("fake", "text", require_images=True)


def test_decision_only_provider_cannot_be_main_agent(fake_registration):
    assert in_process_provider_ids() == (
        "qwen",
        "moonshot",
        "openai",
        "anthropic",
        "google",
        "deepseek",
        "openrouter",
    )
    for provider_id in ("typesafe", "fake"):
        with pytest.raises(ValueError, match="unknown provider"):
            validate_model_ref(f"{provider_id}/model")
        with pytest.raises(ValueError, match="unknown provider"):
            make_chat_provider(provider_id, "model")
    with pytest.raises(ValueError, match="unknown decision provider"):
        make_decision_provider("qwen", "chat-only")


@pytest.mark.parametrize(
    "vendor,model,url",
    [
        ("typesafe", "jev-next", "https://api.typesafe.ai/v1/systemone"),
        ("liquid", "d1-next", "https://api.liquid.ai/decisions/v1/systemone"),
        ("openai", "future-model", "https://api.openai.com/v1/decisions"),
        ("openrouter", "vendor/future", "https://openrouter.ai/api/alpha/decisions"),
        ("openrouter", "cloudflare/clef", "https://openrouter.ai/api/alpha/decisions"),
    ],
)
@pytest.mark.parametrize("with_image", [False, True])
async def test_model_ids_pass_through_and_api_rejection_never_switches_service(
    vendor,
    model,
    url,
    with_image,
    decision_request,
    image_decision_request,
    monkeypatch,
    respx_mock,
):
    monkeypatch.setenv(f"{vendor.upper()}_API_KEY", "fixture-only")
    provider = make_decision_provider(vendor, model, require_images=with_image)
    request = image_decision_request if with_image else decision_request
    route = respx_mock.post(url).respond(404)
    deadline = asyncio.get_running_loop().time() + 5
    try:
        with pytest.raises(ProviderPermanentError, match="HTTP 404"):
            await provider.evaluate(request, deadline=deadline)
        assert route.call_count == 1
        answer = {
            "type": "choice",
            "choice": "YIELD",
            "probabilities": {"YIELD": 1.0, "a1": 0.0},
        }
        if vendor == "openai":
            answer["name"] = "next_action"
            answer["probabilities"] = [
                {"value": key, "probability": value}
                for key, value in answer["probabilities"].items()
            ]
        route.respond(
            json={
                "model": "resolved-version",
                "answers": [answer] if vendor == "openai" else {"next_action": answer},
            }
        )
        result = await provider.evaluate(request, deadline=deadline)
        assert result.requested_model_id == model
        assert result.model_id == "resolved-version"
        sent = route.calls[-1].request
        if vendor == "typesafe" and with_image:
            assert f'"model": "{model}"'.encode() in sent.content
            assert request.context.frame.data in sent.content
        else:
            body = json.loads(sent.content)
            assert body["model"] == model
            if with_image:
                assert b"data:image/jpeg;base64," in sent.content
        assert len(respx_mock.calls) == route.call_count == 2
    finally:
        await provider.aclose()


def test_registration_cannot_overwrite_vendor(fake_registration):
    with pytest.raises(ValueError, match="already registered"):
        register_decision_provider("fake", fake_registration)
    with pytest.raises(ValueError, match="ID is required"):
        register_decision_provider("", fake_registration)


@pytest.mark.parametrize("model", ["", " "])
def test_no_implicit_model(model, fake_registration):
    with pytest.raises(ProviderConfigurationError, match="ID is required"):
        make_decision_provider("fake", model)


def test_decision_key_status_is_shared_and_masked(monkeypatch):
    monkeypatch.setattr(config, "CONFIG", config.Config())
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert provider_key_status("typesafe") == (None, None)
    monkeypatch.setattr(config.CONFIG.provider, "typesafe_api_key", "config-secret")
    assert provider_key_status("typesafe") == (
        "********",
        "config.toml [provider] typesafe_api_key",
    )
    monkeypatch.setenv("TYPESAFE_API_KEY", "env-secret")
    assert provider_key_status("typesafe") == ("********", "TYPESAFE_API_KEY env")


def test_typesafe_credential_is_valid_config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[provider]\ntypesafe_api_key = "fixture-only"\n')
    loaded = config.load(path)
    assert loaded.provider.typesafe_api_key == "fixture-only"
