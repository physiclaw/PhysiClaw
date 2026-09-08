"""OpenAI — native OpenAI endpoint. Pure declaration.

Provider id is `openai` (matches OpenClaw / Anthropic-SDK convention —
the API surface, not the brand `chatgpt`). Reasoning models (gpt-5,
o4-mini) surface chain-of-thought via API field, not a prompt wrapper —
so no `SYSTEM_PROMPT_FRAGMENT`.

Auth: `OPENAI_API_KEY` env, or `[provider] openai_api_key` in
`~/.physiclaw/config.toml`.

Thinking: the reasoning models take `reasoning_effort`. The gpt-5
family accepts `minimal`, which is what "off" means there (no
reasoning tokens); the o-series rejects it and floors at `low`. A
non-reasoning model rejects the field, so it gets nothing.
"""

from typing import Any

from physiclaw.contract.dto import Thinking
from physiclaw.provider.openai_compat import OpenAICompatibleProvider


class OpenAIProvider(OpenAICompatibleProvider):
    PROVIDER_ID = "openai"
    BASE_URL = "https://api.openai.com/v1"

    def thinking_params(self, thinking: Thinking) -> dict[str, Any]:
        if self.model.startswith("gpt-5"):
            return {"reasoning_effort": "minimal" if thinking == "off" else thinking}
        if self.model.startswith(("o1", "o3", "o4")):
            return {"reasoning_effort": "low" if thinking == "off" else thinking}
        return {}
