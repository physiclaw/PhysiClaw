"""Service kind is explicit; a vendor/model name cannot switch APIs."""

from dataclasses import replace

import pytest

from physiclaw.common.provider_errors import ProviderConfigurationError
from physiclaw.contract.decision import DecisionCapabilities
from physiclaw.provider import (
    BaseChatProvider,
    BaseDecisionProvider,
    DecisionProviderSpec,
    OpenAIChatProvider,
    OpenAIDecisionProvider,
    VendorServices,
    make_chat_provider,
    make_decision_provider,
    provider_capabilities,
    register_decision_provider,
    registry,
)
from physiclaw.provider.vendors.typesafe.decision import TypeSafeDecisionProvider


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch):
    monkeypatch.setattr(registry, "_VENDORS", dict(registry._VENDORS))


def test_vendor_can_have_both_api_services():
    assert provider_capabilities("openai") == {"chat", "decision"}
    assert registry._VENDORS["openai"].chat is OpenAIChatProvider
    assert registry._VENDORS["openai"].decision.create is OpenAIDecisionProvider
    assert provider_capabilities("typesafe") == {"decision"}
    assert provider_capabilities("qwen") == {"chat"}
    assert provider_capabilities("unknown") == set()


async def test_same_vendor_and_model_require_explicit_service_selection(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-only")
    chat = make_chat_provider("openai", "gpt-6-luna")
    decision = make_decision_provider("openai", "gpt-6-luna")
    try:
        assert isinstance(chat, BaseChatProvider)
        assert isinstance(decision, BaseDecisionProvider)
        assert chat.api_kind == "chat"
        assert decision.api_kind == "decision"
        assert chat.ref == decision.ref == "openai/gpt-6-luna"
        assert chat._client is not decision._client
        assert (
            chat._client.headers["authorization"]
            == decision._client.headers["authorization"]
        )
        assert not hasattr(chat, "evaluate")
        assert not hasattr(decision, "chat")
        await decision.aclose()
        assert not chat._client.is_closed
    finally:
        await chat.aclose()
        await decision.aclose()


def test_wrong_service_registration_fails_before_client_creation(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("wrong service must fail before client construction")

    monkeypatch.setattr("httpx.AsyncClient", forbidden)
    wrong = DecisionProviderSpec(
        OpenAIChatProvider, lambda model: DecisionCapabilities(), (), ""
    )
    with pytest.raises(ProviderConfigurationError, match="only the decision API"):
        register_decision_provider("new", wrong)
    assert "new" not in registry._VENDORS


@pytest.mark.parametrize("kind", ["chat", "decision"])
def test_corrupted_slot_cannot_swap_service_at_factory(kind, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("wrong service must fail before client construction")

    monkeypatch.setattr("httpx.AsyncClient", forbidden)
    vendor = registry._VENDORS["openai"]
    if kind == "chat":
        registry._VENDORS["openai"] = replace(vendor, chat=OpenAIDecisionProvider)
        factory = make_chat_provider
    else:
        registry._VENDORS["openai"] = replace(
            vendor, decision=replace(vendor.decision, create=OpenAIChatProvider)
        )
        factory = make_decision_provider
    with pytest.raises(ProviderConfigurationError, match=f"only the {kind} API"):
        factory("openai", "gpt-6-luna")


def test_one_vendor_cannot_silently_use_another_vendors_adapter(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("wrong vendor must fail before client construction")

    monkeypatch.setattr("httpx.AsyncClient", forbidden)
    vendor = registry._VENDORS["openai"]
    registry._VENDORS["openai"] = replace(
        vendor, decision=replace(vendor.decision, create=TypeSafeDecisionProvider)
    )
    with pytest.raises(ProviderConfigurationError, match="vendor identity mismatch"):
        make_decision_provider("openai", "gpt-6-luna")


def test_adding_decision_service_preserves_chat_and_credentials():
    class QwenDecisionProvider(BaseDecisionProvider):
        PROVIDER_ID = "qwen"

    chat = registry._VENDORS["qwen"].chat
    spec = DecisionProviderSpec(
        QwenDecisionProvider,
        lambda model: DecisionCapabilities(),
        chat._env_vars(),
        chat._config_key(),
    )
    register_decision_provider("qwen", spec)
    assert registry._VENDORS["qwen"] == VendorServices(chat=chat, decision=spec)


def test_same_vendor_rejects_divergent_credential_settings():
    class QwenDecisionProvider(BaseDecisionProvider):
        PROVIDER_ID = "qwen"

    spec = DecisionProviderSpec(
        QwenDecisionProvider,
        lambda model: DecisionCapabilities(),
        ("OTHER_VENDOR_KEY",),
        "other_vendor_key",
    )
    with pytest.raises(ProviderConfigurationError, match="share credential settings"):
        register_decision_provider("qwen", spec)
    assert registry._VENDORS["qwen"].decision is None


def test_unsupported_service_or_model_does_not_fall_back(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("unsupported service/model must not create a fallback client")

    monkeypatch.setattr("httpx.AsyncClient", forbidden)
    with pytest.raises(ValueError, match="unknown provider"):
        make_chat_provider("typesafe", "jev-1.14.0")
    with pytest.raises(ValueError, match="unknown decision provider"):
        make_decision_provider("anthropic", "chat-model")
    with pytest.raises(
        ProviderConfigurationError, match="unsupported OpenAI decision model"
    ):
        make_decision_provider("openai", "gpt-6-astra")
