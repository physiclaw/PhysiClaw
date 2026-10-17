"""Model providers: engine DTOs in, normalized assistant messages out.

Use ``make_provider(provider_id, model_id)`` and close the returned
provider with ``aclose()``. ``BaseProvider.chat`` owns usage accounting;
its wire-specific ``_chat`` implementation performs one model call.
The engine owns retries through ``ProviderTransientError`` (transport,
rate limits, server failures, malformed completions) and
``ProviderPermanentError`` (requests that need correction).

``openai_compat`` implements Chat Completions for OpenAI, Qwen,
Moonshot, DeepSeek, and Google's compatibility endpoint.
``anthropic_compat`` implements Messages through Anthropic's SDK.
``vendors`` declare endpoints, thinking settings, and protocol quirks.
``wire`` shares content encoders; ``registry`` resolves provider IDs;
``discovered`` keeps a best-effort cache of accessible model IDs.

``serialize_history`` creates fresh wire messages and places cache
markers. Reasoning stays outside public assistant text; opaque state
required for tool continuations lives in ``vendor_extra`` and is
replayed only by its originating provider. Invalid completions fail
before partial tools can execute. Optional accounting and cache-write
failures must not discard a usable model reply or discovery result.

A model ref is ``provider/model`` (for example ``qwen/qwen3.6-plus``).
Every registered provider runs through the PhysiClaw engine. This module
is the public re-export surface.
"""

from physiclaw.provider.anthropic_compat import AnthropicCompatibleProvider
from physiclaw.provider.openai_compat import OpenAICompatibleProvider
from physiclaw.provider.provider_base import (
    BaseProvider,
    Provider,
    ProviderConfigurationError,
    ProviderError,
    ProviderPermanentError,
    ProviderTransientError,
)
from physiclaw.provider.registry import (
    in_process_provider_ids,
    is_known,
    make_provider,
    provider_class,
    provider_key_status,
    validate_model_ref,
)
from physiclaw.provider.vendors.anthropic import AnthropicProvider
from physiclaw.provider.vendors.deepseek import DeepSeekProvider
from physiclaw.provider.vendors.google import GoogleProvider
from physiclaw.provider.vendors.moonshot import MoonshotProvider
from physiclaw.provider.vendors.openai import OpenAIProvider
from physiclaw.provider.vendors.qwen import QwenProvider
from physiclaw.provider.wire import (
    assistant_to_wire,
    mcp_blocks_to_content_blocks,
    tool_result_to_wire,
    tool_to_wire,
    user_content_to_openai,
)

__all__ = [
    "AnthropicCompatibleProvider",
    "AnthropicProvider",
    "BaseProvider",
    "DeepSeekProvider",
    "GoogleProvider",
    "MoonshotProvider",
    "OpenAICompatibleProvider",
    "OpenAIProvider",
    "Provider",
    "ProviderConfigurationError",
    "ProviderError",
    "ProviderPermanentError",
    "ProviderTransientError",
    "QwenProvider",
    "assistant_to_wire",
    "in_process_provider_ids",
    "is_known",
    "make_provider",
    "mcp_blocks_to_content_blocks",
    "provider_class",
    "provider_key_status",
    "validate_model_ref",
    "tool_result_to_wire",
    "tool_to_wire",
    "user_content_to_openai",
]
