"""Public provider construction and metadata API.

Consumers import generation interfaces from physiclaw.contract.chat and
physiclaw.contract.decision. Adapter implementations and registration live
in their own modules; callers never build native requests.

Each call makes one inference attempt. Providers own API translation,
cache markers, opaque replay and usage normalization. Producers own context,
schemas, retries and interpretation; the host validates and executes actions.
"""

from physiclaw.provider.catalog import cache as discovery
from physiclaw.provider.services.factory import (
    make_chat_catalog,
    make_chat_provider,
    make_decision_provider,
    provider_key_status,
)
from physiclaw.provider.services.registry import (
    chat_provider_ids,
    decision_provider_capabilities,
    provider_capabilities,
    validate_chat_model_ref,
)

__all__ = [
    "decision_provider_capabilities",
    "discovery",
    "chat_provider_ids",
    "make_chat_catalog",
    "make_chat_provider",
    "make_decision_provider",
    "provider_capabilities",
    "provider_key_status",
    "validate_chat_model_ref",
]
