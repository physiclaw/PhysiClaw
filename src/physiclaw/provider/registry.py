"""One vendor identity, explicit chat and decision service registrations.

Factories select an API capability, never infer it from a model name and
never fall back to the other service. Metadata lookups construct no client.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal, Protocol

from physiclaw.contract.decision import DecisionCapabilities, DecisionProvider
from physiclaw.contract.plugin import EventSink
from physiclaw.provider.chat.base import BaseChatProvider, ChatProvider
from physiclaw.provider.provider_base import ProviderConfigurationError
from physiclaw.provider.vendors.anthropic.chat import AnthropicChatProvider
from physiclaw.provider.vendors.deepseek.chat import DeepSeekChatProvider
from physiclaw.provider.vendors.google.chat import GoogleChatProvider
from physiclaw.provider.vendors.moonshot.chat import MoonshotChatProvider
from physiclaw.provider.vendors.openai.chat import OpenAIChatProvider
from physiclaw.provider.vendors.openai.decision import OpenAIDecisionProvider
from physiclaw.provider.vendors.qwen.chat import QwenChatProvider
from physiclaw.provider.vendors.typesafe.decision import TypeSafeDecisionProvider


class _DecisionFactory(Protocol):
    @property
    def api_kind(self) -> Literal["decision"]: ...

    def __call__(self, model: str, *, timeout: float) -> DecisionProvider: ...


@dataclass(frozen=True)
class DecisionProviderSpec:
    """A decision adapter constructor and its credential/capability metadata."""

    create: _DecisionFactory
    capabilities: Callable[[str], DecisionCapabilities]
    key_env_vars: tuple[str, ...]
    config_key: str


@dataclass(frozen=True)
class VendorServices:
    """A vendor can provide either service or both through separate adapters."""

    chat: type[BaseChatProvider] | None = None
    decision: DecisionProviderSpec | None = None


_VENDORS: dict[str, VendorServices] = {
    "qwen": VendorServices(chat=QwenChatProvider),
    "moonshot": VendorServices(chat=MoonshotChatProvider),
    "openai": VendorServices(
        chat=OpenAIChatProvider,
        decision=DecisionProviderSpec(
            OpenAIDecisionProvider,
            OpenAIDecisionProvider.capabilities_for,
            OpenAIDecisionProvider._env_vars(),
            OpenAIDecisionProvider._config_key(),
        ),
    ),
    "anthropic": VendorServices(chat=AnthropicChatProvider),
    "google": VendorServices(chat=GoogleChatProvider),
    "deepseek": VendorServices(chat=DeepSeekChatProvider),
    "typesafe": VendorServices(
        decision=DecisionProviderSpec(
            TypeSafeDecisionProvider,
            TypeSafeDecisionProvider.capabilities_for,
            TypeSafeDecisionProvider._env_vars(),
            TypeSafeDecisionProvider._config_key(),
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
    if vendor.chat is not None and (
        spec.key_env_vars != vendor.chat._env_vars()
        or spec.config_key != vendor.chat._config_key()
    ):
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


def make_decision_provider(
    provider_id: str, model_id: str, *, require_images: bool = False
) -> DecisionProvider:
    """Construct only a decision service; never substitute a chat adapter."""
    from physiclaw.common.config import CONFIG

    vendor = _VENDORS.get(provider_id)
    if vendor is None or vendor.decision is None:
        raise ValueError(f"unknown decision provider {provider_id!r}")
    spec = vendor.decision
    _require_kind(spec.create, "decision", provider_id)
    if not isinstance(model_id, str) or not model_id.strip():
        raise ProviderConfigurationError("a decision model ID is required")
    capabilities = spec.capabilities(model_id)
    if require_images and not capabilities.images:
        raise ProviderConfigurationError(
            "decision provider/model does not support images"
        )
    return spec.create(model_id, timeout=CONFIG.engine.provider_timeout_seconds)


def in_process_provider_ids() -> tuple[str, ...]:
    """Chat service IDs in existing menu order; decision-only vendors excluded."""
    return tuple(
        provider_id
        for provider_id, vendor in _VENDORS.items()
        if vendor.chat is not None
    )


def is_known(provider_id: str) -> bool:
    """Compatibility lookup for a known chat provider."""
    return provider_class(provider_id) is not None


def provider_class(provider_id: str) -> type[BaseChatProvider] | None:
    vendor = _VENDORS.get(provider_id)
    return vendor.chat if vendor else None


def provider_key_status(provider_id: str) -> tuple[str | None, str | None]:
    """Shared vendor key status, always masked; no client construction."""
    from physiclaw.common.config import resolve_provider_key

    vendor = _VENDORS.get(provider_id)
    if vendor is None:
        return None, None
    if vendor.chat is not None:
        env_vars, config_key = vendor.chat._env_vars(), vendor.chat._config_key()
    elif vendor.decision is not None:
        env_vars, config_key = vendor.decision.key_env_vars, vendor.decision.config_key
    else:
        return None, None
    value, source = resolve_provider_key(env_vars, config_key)
    return ("********" if value else None), source


def validate_model_ref(ref: str) -> tuple[str, str]:
    """Validate a chat model reference; model IDs do not select API kind."""
    from physiclaw.common.model_ref import parse_model_ref

    provider_id, model_id = parse_model_ref(ref)
    if provider_class(provider_id) is None:
        raise ValueError(
            f"unknown provider {provider_id!r} in ref {ref!r} (known: {', '.join(in_process_provider_ids())})"
        )
    return provider_id, model_id


def make_chat_provider(
    provider_id: str, model_id: str, *, usage_sink: EventSink | None = None
) -> ChatProvider:
    """Construct only a chat service; never substitute a decision adapter."""
    from physiclaw.common.config import CONFIG

    cls = provider_class(provider_id)
    if cls is None:
        raise ValueError(
            f"unknown provider {provider_id!r} (known: {', '.join(in_process_provider_ids())})"
        )
    _require_kind(cls, "chat", provider_id)
    return cls(
        model=model_id,
        timeout=CONFIG.engine.provider_timeout_seconds,
        usage_sink=usage_sink,
    )
