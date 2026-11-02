"""Explicit chat and decision API services, grouped by vendor.

Providers own credentials, client lifecycle, API encoding, cache markers,
opaque replay, response normalization and usage accounting. Chat and decision
adapters have separate contracts. Each call makes one transport attempt.

Callers own prompts, history, output schemas, retries and action execution.
They use shared contract DTOs; native request snapshots are diagnostics only.

Use make_chat_provider or make_decision_provider to select the API kind.
Each service owns its client and context; neither factory substitutes the
other service.
"""

from physiclaw.contract.decision import DecisionProvider
from physiclaw.provider.chat import discovered
from physiclaw.provider.chat.anthropic_compat import AnthropicCompatibleProvider
from physiclaw.provider.chat.base import BaseChatProvider, ChatProvider
from physiclaw.provider.chat.openai_compat import OpenAICompatibleProvider
from physiclaw.provider.decision.base import BaseDecisionProvider
from physiclaw.provider.provider_base import BaseAPIProvider
from physiclaw.provider.registry import (
    DecisionProviderSpec,
    VendorServices,
    decision_provider_capabilities,
    in_process_provider_ids,
    is_known,
    make_chat_provider,
    make_decision_provider,
    provider_capabilities,
    provider_class,
    provider_key_status,
    register_decision_provider,
    validate_model_ref,
)
from physiclaw.provider.vendors.anthropic.chat import AnthropicChatProvider
from physiclaw.provider.vendors.deepseek.chat import DeepSeekChatProvider
from physiclaw.provider.vendors.google.chat import GoogleChatProvider
from physiclaw.provider.vendors.liquid.decision import LiquidDecisionProvider
from physiclaw.provider.vendors.moonshot.chat import MoonshotChatProvider
from physiclaw.provider.vendors.openai.chat import OpenAIChatProvider
from physiclaw.provider.vendors.openai.decision import OpenAIDecisionProvider
from physiclaw.provider.vendors.openrouter.chat import OpenRouterChatProvider
from physiclaw.provider.vendors.openrouter.decision import OpenRouterDecisionProvider
from physiclaw.provider.vendors.qwen.chat import QwenChatProvider
from physiclaw.provider.vendors.typesafe.decision import TypeSafeDecisionProvider

__all__ = [
    "AnthropicChatProvider",
    "AnthropicCompatibleProvider",
    "BaseAPIProvider",
    "BaseChatProvider",
    "BaseDecisionProvider",
    "ChatProvider",
    "DecisionProvider",
    "DecisionProviderSpec",
    "VendorServices",
    "DeepSeekChatProvider",
    "GoogleChatProvider",
    "LiquidDecisionProvider",
    "MoonshotChatProvider",
    "OpenAIChatProvider",
    "OpenAICompatibleProvider",
    "OpenAIDecisionProvider",
    "OpenRouterChatProvider",
    "OpenRouterDecisionProvider",
    "QwenChatProvider",
    "TypeSafeDecisionProvider",
    "decision_provider_capabilities",
    "discovered",
    "in_process_provider_ids",
    "is_known",
    "make_chat_provider",
    "make_decision_provider",
    "provider_capabilities",
    "provider_class",
    "provider_key_status",
    "register_decision_provider",
    "validate_model_ref",
]
