"""Anthropic — Claude models via the official `anthropic` SDK.

Distinct from `claude-code` (the subprocess engine that shells out to
the `claude` CLI). This provider calls Anthropic's `/v1/messages`
endpoint directly through `AsyncAnthropic` — wire-format flow lives in
`provider/anthropic_compat.py`.

Auth: `ANTHROPIC_API_KEY` env, or `[provider] anthropic_api_key` in
`~/.physiclaw/config.toml`.

Claude 4.6 and later use adaptive thinking and effort. Earlier models
keep the wire base's fixed token budgets. "off" uses the lowest mode
the model permits; some models always think, and Sonnet 5.5 can only
disable thinking before tool calls. Unknown IDs retain the base behavior.
"""

import re
from typing import Any

from physiclaw.contract.dto import Thinking
from physiclaw.provider.anthropic_compat import AnthropicCompatibleProvider

# Exact aliases, with optional dated snapshots stripped below. An
# unrecognized future model must not inherit a guessed capability.
# https://platform.claude.com/docs/en/build-with-claude/thinking
_ADAPTIVE_OFF_MODES = {
    "claude-opus-4-6": "disabled",
    "claude-sonnet-4-6": "disabled",
    "claude-opus-4-7": "disabled",
    "claude-opus-4-8": "disabled",
    "claude-opus-5": "disabled",
    "claude-sonnet-5": "disabled",
    "claude-haiku-5-5": "disabled",
    "claude-sonnet-5-5": "between_tools",
    "claude-opus-5-5": "adaptive",
    "claude-fable-5": "adaptive",
    "claude-fable-5-1": "adaptive",
    "claude-mythos-5": "adaptive",
    "claude-mythos-5-1": "adaptive",
    "claude-mythos-preview": "adaptive",
}


class AnthropicProvider(AnthropicCompatibleProvider):
    PROVIDER_ID = "anthropic"
    # Informational only — `AsyncAnthropic` manages the URL itself.
    BASE_URL = "https://api.anthropic.com/v1"
    # INLINE_TOOL_INDEX stays default-True: Claude reads the native
    # `tools=` schema fine, but the ~800-token card is kept as cheap
    # insurance that every tool stays visible in SYSTEM.

    def thinking_params(self, thinking: Thinking) -> dict[str, Any]:
        model = re.sub(r"-\d{8}$", "", self.model)
        off_mode = _ADAPTIVE_OFF_MODES.get(model)
        params = super().thinking_params(thinking)
        if off_mode is None:
            return params
        return {
            **params,
            "thinking": {"type": off_mode if thinking == "off" else "adaptive"},
            "output_config": {"effort": "low" if thinking == "off" else thinking},
        }
