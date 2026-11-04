"""Application construction boundary for independently owned API services.

Resolve runtime defaults here. Registration and capability inspection stay
offline in registry; construction sends no network requests.
"""

from physiclaw.common.provider_errors import ProviderConfigurationError
from physiclaw.contract.chat import ChatProvider, ModelCatalog
from physiclaw.contract.decision import DecisionProvider
from physiclaw.contract.plugin import EventSink
from physiclaw.provider.services import registry


def make_chat_provider(
    provider_id: str, model_id: str, *, usage_sink: EventSink | None = None
) -> ChatProvider:
    """Construct only a chat service; never substitute a decision adapter."""
    from physiclaw.common.config import CONFIG

    cls = registry.provider_class(provider_id)
    if cls is None:
        raise ValueError(
            f"unknown provider {provider_id!r} (known: {', '.join(registry.chat_provider_ids())})"
        )
    registry._require_kind(cls, "chat", provider_id)
    return cls(
        model=model_id,
        timeout=CONFIG.engine.provider_timeout_seconds,
        usage_sink=usage_sink,
        max_output_tokens=CONFIG.engine.provider_max_output_tokens or None,
    )


def make_decision_provider(
    provider_id: str, model_id: str, *, require_images: bool = False
) -> DecisionProvider:
    """Construct only a decision service; never substitute a chat adapter."""
    from physiclaw.common.config import CONFIG

    vendor = registry._VENDORS.get(provider_id)
    if vendor is None or vendor.decision is None:
        raise ValueError(f"unknown decision provider {provider_id!r}")
    spec = vendor.decision
    registry._require_kind(spec.create, "decision", provider_id)
    if not isinstance(model_id, str) or not model_id.strip():
        raise ProviderConfigurationError("a decision model ID is required")
    capabilities = spec.capabilities(model_id)
    if require_images and not capabilities.images:
        raise ProviderConfigurationError(
            "decision provider/model does not support images"
        )
    return spec.create(model_id, timeout=CONFIG.engine.provider_timeout_seconds)


def make_chat_catalog(provider_id: str) -> ModelCatalog:
    """Create an owned discovery client without sending a request."""
    cls = registry.provider_class(provider_id)
    if cls is None:
        raise ValueError(f"unknown provider {provider_id!r}")
    registry._require_kind(cls, "chat", provider_id)
    return cls(model="")


def provider_key_status(provider_id: str) -> tuple[str | None, str | None]:
    """Shared vendor key status, always masked; no client construction."""
    vendor = registry._VENDORS.get(provider_id)
    credentials = vendor.credentials if vendor is not None else None
    if credentials is None:
        return None, None
    value, source = credentials.resolve()
    return ("********" if value else None), source
