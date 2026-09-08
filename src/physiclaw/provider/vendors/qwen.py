"""Qwen / DashScope — OpenAI-compatible endpoint. Pure declaration.

Uses native tool_calls. The DashScope `compatible-mode` endpoint accepts
the standard OpenAI request shape; Qwen-specific details are the
`reasoning_content` field on responses (handled in
`OpenAICompatibleProvider._parse_response`) and the `<think>...</think>`
system-prompt fragment declared below.

Auth: `QWEN_API_KEY` / `DASHSCOPE_API_KEY` env, or
`[provider] qwen_api_key` in `~/.physiclaw/config.toml`.
"""

from typing import Any

from physiclaw.contract.dto import Thinking
from physiclaw.provider.openai_compat import OpenAICompatibleProvider
from physiclaw.provider.provider_base import THINKING_BUDGETS

# The hybrid models switch thinking with `enable_thinking` and bound it
# with `thinking_budget`; a model without the switch rejects the field,
# so it gets nothing.
_HYBRID_MODELS = ("qwen3", "qwen-plus", "qwen-flash", "qwen-turbo")


class QwenProvider(OpenAICompatibleProvider):
    PROVIDER_ID = "qwen"
    BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    API_KEY_ENV_VARS = ("QWEN_API_KEY", "DASHSCOPE_API_KEY")
    SYSTEM_PROMPT_FRAGMENT = (
        "Wrap internal reasoning in `<think>...</think>`. Anything outside "
        "`<think>` is interpreted as either a tool call or a user-visible reply.\n"
        "Never put reasoning inside tool arguments — handlers receive `args` "
        "raw, not your scratchpad."
    )

    def thinking_params(self, thinking: Thinking) -> dict[str, Any]:
        if not self.model.startswith(_HYBRID_MODELS):
            return {}
        if thinking == "off":
            return {"enable_thinking": False}
        return {"enable_thinking": True, "thinking_budget": THINKING_BUDGETS[thinking]}
