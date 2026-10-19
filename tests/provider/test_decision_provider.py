"""A second implementation must fit without TypeSafe-shaped scores or state."""

import asyncio

import pytest

from physiclaw.common import config
from physiclaw.contract.decision import DecisionCapabilities, DecisionResult
from physiclaw.provider import (
    DecisionProviderSpec,
    ProviderConfigurationError,
    decision_provider_capabilities,
    in_process_provider_ids,
    make_decision_provider,
    make_provider,
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
            request.id, request.candidates[1].id, "fake", self.model, self.model
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
    assert result.selected_id == "a1"
    assert result.provider_id == "fake"
    assert result.confidence is result.probabilities is None
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
        decision_provider_capabilities("typesafe", "unverified-version").images is False
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
    )
    for provider_id in ("typesafe", "fake"):
        with pytest.raises(ValueError, match="unknown provider"):
            validate_model_ref(f"{provider_id}/model")
        with pytest.raises(ValueError, match="unknown provider"):
            make_provider(provider_id, "model")
    with pytest.raises(ValueError, match="unknown decision provider"):
        make_decision_provider("qwen", "chat-only")
    with pytest.raises(
        ProviderConfigurationError, match="unsupported OpenAI decision model"
    ):
        make_decision_provider("openai", "chat-only")


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
