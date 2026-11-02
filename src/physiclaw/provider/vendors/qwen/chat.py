"""Qwen / DashScope — OpenAI-compatible endpoint and request options.

Uses native tool_calls. The DashScope `compatible-mode` endpoint accepts
the standard OpenAI request shape; Qwen-specific details are the
`reasoning_content` field on responses (handled in
`OpenAICompatibleProvider._parse_response`) and the thinking request
options declared below.

Auth: `QWEN_API_KEY` / `DASHSCOPE_API_KEY` env, or
`[provider] qwen_api_key` in `~/.physiclaw/config.toml`.
"""

from typing import Any

from physiclaw.contract.dto import Thinking
from physiclaw.contract.structured import OutputSpec
from physiclaw.provider.chat.base import THINKING_BUDGETS
from physiclaw.provider.chat.openai_compat import OpenAICompatibleProvider

# The hybrid models switch thinking with `enable_thinking` and bound it
# with `thinking_budget`; a model without the switch rejects the field,
# so it gets nothing.
_HYBRID_MODELS = ("qwen3", "qwen-plus", "qwen-flash", "qwen-turbo")


class QwenChatProvider(OpenAICompatibleProvider):
    PROVIDER_ID = "qwen"
    BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    API_KEY_ENV_VARS = ("QWEN_API_KEY", "DASHSCOPE_API_KEY")

    def _tool_params(
        self, spec: OutputSpec | None, *, thinking: Thinking | None
    ) -> dict:
        params = super()._tool_params(spec, thinking=thinking)
        if spec is not None and self.model.startswith("qwen"):
            params["parallel_tool_calls"] = False
            if (
                thinking is not None
                and self.thinking_params(thinking).get("enable_thinking") is False
            ):
                params["tool_choice"] = {
                    "type": "function",
                    "function": {"name": spec.function_name},
                }
        return params

    def thinking_params(self, thinking: Thinking) -> dict[str, Any]:
        if not self.model.startswith(_HYBRID_MODELS):
            return {}
        if thinking == "off":
            return {"enable_thinking": False}
        return {"enable_thinking": True, "thinking_budget": THINKING_BUDGETS[thinking]}
