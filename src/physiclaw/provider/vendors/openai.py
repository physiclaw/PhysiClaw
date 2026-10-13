"""OpenAI — native OpenAI endpoint. Pure declaration.

Provider id is `openai` (matches OpenClaw / Anthropic-SDK convention —
the API surface, not the brand `chatgpt`). Reasoning models (gpt-5,
o4-mini) surface chain-of-thought via API field, not a prompt wrapper —
so no `SYSTEM_PROMPT_FRAGMENT`.

Auth: `OPENAI_API_KEY` env, or `[provider] openai_api_key` in
`~/.physiclaw/config.toml`.

Thinking: original GPT-5 models floor at `minimal`; later GPT-5
models support `none`. These are distinct API values, and `minimal`
does not guarantee zero reasoning tokens. The o-series floors at
`low`. Models outside the known table keep their API default.
"""

import re
from typing import Any

from physiclaw.contract.dto import Thinking
from physiclaw.provider.openai_compat import OpenAICompatibleProvider

# Exact aliases plus their dated snapshots. Do not infer capabilities
# for chat/codex/pro variants from a shared prefix.
_MINIMAL_MODELS = {"gpt-5", "gpt-5-mini", "gpt-5-nano"}
_NONE_MODELS = {"gpt-5.1", "gpt-5.2", "gpt-5.4", "gpt-5.5"}


class OpenAIProvider(OpenAICompatibleProvider):
    PROVIDER_ID = "openai"
    BASE_URL = "https://api.openai.com/v1"

    def thinking_params(self, thinking: Thinking) -> dict[str, Any]:
        model = re.sub(r"-\d{4}-\d{2}-\d{2}$", "", self.model)
        if model in _MINIMAL_MODELS:
            return {"reasoning_effort": "minimal" if thinking == "off" else thinking}
        if model in _NONE_MODELS:
            return {"reasoning_effort": "none" if thinking == "off" else thinking}
        if self.model.startswith(("o1", "o3", "o4")):
            return {"reasoning_effort": "low" if thinking == "off" else thinking}
        return {}
