"""Explicit chat and decision API services, grouped by vendor.

provider_base owns credentials and client lifecycle. chat/ owns history,
cache/accounting and generation protocols. decision/ owns bounded choices.
vendors/<vendor>/chat.py and decision.py contain service-specific adapters.

Use make_chat_provider or make_decision_provider to select the API kind.
Each service owns its client and context; neither factory substitutes the
other service. Legacy Provider/BaseProvider/make_provider names remain
chat-only aliases for existing consumers.
"""

from physiclaw.contract.decision import DecisionProvider
from physiclaw.provider.chat import discovered
from physiclaw.provider.chat.anthropic_compat import AnthropicCompatibleProvider
from physiclaw.provider.chat.base import BaseChatProvider, ChatProvider
from physiclaw.provider.chat.openai_compat import OpenAICompatibleProvider
from physiclaw.provider.chat.wire import (
    assistant_to_wire,
    mcp_blocks_to_content_blocks,
    tool_result_to_wire,
    tool_to_wire,
    user_content_to_openai,
)
from physiclaw.provider.decision.decision_compat import BaseDecisionProvider
from physiclaw.provider.provider_base import (
    BaseAPIProvider,
    ProviderConfigurationError,
    ProviderError,
    ProviderPermanentError,
    ProviderTransientError,
)
from physiclaw.provider.registry import (
    DecisionProviderSpec,
    VendorServices,
    decision_provider_capabilities,
    in_process_provider_ids,
    is_known,
    make_chat_provider,
    make_decision_provider,
    make_provider,
    provider_capabilities,
    provider_class,
    provider_key_status,
    register_decision_provider,
    validate_model_ref,
)
from physiclaw.provider.vendors.anthropic.chat import AnthropicChatProvider
from physiclaw.provider.vendors.deepseek.chat import DeepSeekChatProvider
from physiclaw.provider.vendors.google.chat import GoogleChatProvider
from physiclaw.provider.vendors.moonshot.chat import MoonshotChatProvider
from physiclaw.provider.vendors.openai.chat import OpenAIChatProvider
from physiclaw.provider.vendors.openai.decision import OpenAIDecisionProvider
from physiclaw.provider.vendors.qwen.chat import QwenChatProvider
from physiclaw.provider.vendors.typesafe.decision import TypeSafeDecisionProvider

# Compatibility names always mean chat, including for a dual-service vendor.
Provider = ChatProvider
BaseProvider = BaseChatProvider
AnthropicProvider = AnthropicChatProvider
DeepSeekProvider = DeepSeekChatProvider
GoogleProvider = GoogleChatProvider
MoonshotProvider = MoonshotChatProvider
OpenAIProvider = OpenAIChatProvider
QwenProvider = QwenChatProvider

__all__ = [
    "AnthropicChatProvider",
    "AnthropicCompatibleProvider",
    "AnthropicProvider",
    "BaseAPIProvider",
    "BaseChatProvider",
    "BaseDecisionProvider",
    "BaseProvider",
    "ChatProvider",
    "DecisionProvider",
    "DecisionProviderSpec",
    "VendorServices",
    "DeepSeekChatProvider",
    "DeepSeekProvider",
    "GoogleChatProvider",
    "GoogleProvider",
    "MoonshotChatProvider",
    "MoonshotProvider",
    "OpenAIChatProvider",
    "OpenAICompatibleProvider",
    "OpenAIDecisionProvider",
    "OpenAIProvider",
    "Provider",
    "ProviderConfigurationError",
    "ProviderError",
    "ProviderPermanentError",
    "ProviderTransientError",
    "QwenChatProvider",
    "QwenProvider",
    "TypeSafeDecisionProvider",
    "assistant_to_wire",
    "decision_provider_capabilities",
    "discovered",
    "in_process_provider_ids",
    "is_known",
    "make_chat_provider",
    "make_decision_provider",
    "make_provider",
    "mcp_blocks_to_content_blocks",
    "provider_capabilities",
    "provider_class",
    "provider_key_status",
    "register_decision_provider",
    "tool_result_to_wire",
    "tool_to_wire",
    "user_content_to_openai",
    "validate_model_ref",
]
