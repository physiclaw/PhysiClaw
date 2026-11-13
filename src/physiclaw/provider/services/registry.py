"""One vendor identity, explicit chat and decision service registrations.

This module owns offline metadata and registration validation. Construction
lives in factory.py; metadata lookups construct no client.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal, Protocol

from physiclaw.common.provider_errors import ProviderConfigurationError
from physiclaw.contract.decision import DecisionCapabilities, DecisionProvider
from physiclaw.provider.chat.base import BaseChatProvider
from physiclaw.provider.client.configuration import CredentialSpec
from physiclaw.provider.vendors.anthropic.chat import AnthropicChatProvider
from physiclaw.provider.vendors.deepseek.chat import DeepSeekChatProvider
from physiclaw.provider.vendors.google.chat import GoogleChatProvider
from physiclaw.provider.vendors.google.decision import GoogleDecisionProvider
from physiclaw.provider.vendors.liquid.decision import LiquidDecisionProvider
from physiclaw.provider.vendors.moonshot.chat import MoonshotChatProvider
from physiclaw.provider.vendors.openai.chat import OpenAIChatProvider
from physiclaw.provider.vendors.openai.decision import OpenAIDecisionProvider
from physiclaw.provider.vendors.openrouter.chat import OpenRouterChatProvider
from physiclaw.provider.vendors.openrouter.decision import OpenRouterDecisionProvider
from physiclaw.provider.vendors.qwen.chat import QwenChatProvider
from physiclaw.provider.vendors.typesafe.decision import TypeSafeDecisionProvider


class _DecisionFactory(Protocol):
    """Adapter class constructor; registration also checks its API methods."""

    @property
    def api_kind(self) -> Literal["decision"]: ...

    def __call__(self, model: str, *, timeout: float) -> DecisionProvider: ...


@dataclass(frozen=True)
class DecisionProviderSpec:
    """A decision adapter class and its credential/capability metadata."""

    create: _DecisionFactory
    capabilities: Callable[[str], DecisionCapabilities]
    credentials: CredentialSpec


@dataclass(frozen=True)
class VendorServices:
    """A vendor can provide either service or both through separate adapters."""

    chat: type[BaseChatProvider] | None = None
    decision: DecisionProviderSpec | None = None

    @property
    def credentials(self) -> CredentialSpec | None:
        if self.chat is not None:
            return self.chat.credentials()
        return self.decision.credentials if self.decision is not None else None


_VENDORS: dict[str, VendorServices] = {
    "qwen": VendorServices(chat=QwenChatProvider),
    "moonshot": VendorServices(chat=MoonshotChatProvider),
    "openai": VendorServices(
        chat=OpenAIChatProvider,
        decision=DecisionProviderSpec(
            OpenAIDecisionProvider,
            OpenAIDecisionProvider.capabilities_for,
            OpenAIDecisionProvider.credentials(),
        ),
    ),
    "anthropic": VendorServices(chat=AnthropicChatProvider),
    "google": VendorServices(
        chat=GoogleChatProvider,
        decision=DecisionProviderSpec(
            GoogleDecisionProvider,
            GoogleDecisionProvider.capabilities_for,
            GoogleDecisionProvider.credentials(),
        ),
    ),
    "deepseek": VendorServices(chat=DeepSeekChatProvider),
    "typesafe": VendorServices(
        decision=DecisionProviderSpec(
            TypeSafeDecisionProvider,
            TypeSafeDecisionProvider.capabilities_for,
            TypeSafeDecisionProvider.credentials(),
        )
    ),
    "openrouter": VendorServices(
        chat=OpenRouterChatProvider,
        decision=DecisionProviderSpec(
            OpenRouterDecisionProvider,
            OpenRouterDecisionProvider.capabilities_for,
            OpenRouterDecisionProvider.credentials(),
        ),
    ),
    "liquid": VendorServices(
        decision=DecisionProviderSpec(
            LiquidDecisionProvider,
            LiquidDecisionProvider.capabilities_for,
            LiquidDecisionProvider.credentials(),
        )
    ),
}


def _require_kind(
    adapter: object, kind: Literal["chat", "decision"], provider_id: str
) -> None:
    method, other = ("chat", "evaluate") if kind == "chat" else ("evaluate", "chat")
    if (
        getattr(adapter, "api_kind", None) != kind
        or not callable(getattr(adapter, method, None))
        or callable(getattr(adapter, other, None))
    ):
        raise ProviderConfigurationError(
            f"registered adapter must provide only the {kind} API"
        )
    if getattr(adapter, "PROVIDER_ID", provider_id) != provider_id:
        raise ProviderConfigurationError("registered adapter vendor identity mismatch")


def register_decision_provider(provider_id: str, spec: DecisionProviderSpec) -> None:
    """Add one decision service; preserve an existing chat service and key."""
    if not provider_id.strip():
        raise ValueError("a decision provider ID is required")
    vendor = _VENDORS.get(provider_id, VendorServices())
    if vendor.decision is not None:
        raise ValueError("decision provider already registered")
    _require_kind(spec.create, "decision", provider_id)
    if vendor.chat is not None and (spec.credentials != vendor.chat.credentials()):
        raise ProviderConfigurationError(
            "services of one vendor must share credential settings"
        )
    _VENDORS[provider_id] = replace(vendor, decision=spec)


def provider_capabilities(provider_id: str) -> frozenset[str]:
    """Registered service kinds, independent of model or account availability."""
    vendor = _VENDORS.get(provider_id)
    if vendor is None:
        return frozenset()
    return frozenset(
        kind
        for kind, service in (("chat", vendor.chat), ("decision", vendor.decision))
        if service is not None
    )


def decision_provider_capabilities(
    provider_id: str, model_id: str
) -> DecisionCapabilities | None:
    vendor = _VENDORS.get(provider_id)
    return (
        vendor.decision.capabilities(model_id) if vendor and vendor.decision else None
    )


def chat_provider_ids() -> tuple[str, ...]:
    """Chat service IDs in registration order; decision-only vendors excluded."""
    return tuple(
        provider_id
        for provider_id, vendor in _VENDORS.items()
        if vendor.chat is not None
    )


def is_known(provider_id: str) -> bool:
    """Whether a chat adapter is registered for this vendor."""
    return provider_class(provider_id) is not None


def provider_class(provider_id: str) -> type[BaseChatProvider] | None:
    vendor = _VENDORS.get(provider_id)
    return vendor.chat if vendor else None


def validate_chat_model_ref(ref: str) -> tuple[str, str]:
    """Validate a chat model reference; model IDs do not select API kind."""
    from physiclaw.common.model_ref import parse_model_ref

    provider_id, model_id = parse_model_ref(ref)
    if provider_class(provider_id) is None:
        raise ValueError(
            f"unknown provider {provider_id!r} in ref {ref!r} (known: {', '.join(chat_provider_ids())})"
        )
    return provider_id, model_id
