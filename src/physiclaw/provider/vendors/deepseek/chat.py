"""DeepSeek — OpenAI-compatible endpoint. Declaration + one knob.

Models: `deepseek-flash` takes images (`image_url` data URLs, the
shape `wire.py` emits) and is the one that can drive the loop.
`deepseek-v4-pro` is text-only and does not refuse an image: it
answers as if none was sent. Retired ids (`deepseek-v4-flash`,
`deepseek-v4-flash-vision-exp`) are served by Flash.

Caching: automatic prefix caching, no opt-in field — markers buy
nothing (`NO_CACHE_MARKERS`). Image tokens cache too. Hits are
reported as `prompt_tokens_details.cached_tokens` and top-level
`prompt_cache_hit_tokens`; the base `_parse_usage` reads either.
Misses are ordinary-priced input, not cache writes.

Auth: `DEEPSEEK_API_KEY` env, or `[provider] deepseek_api_key` in
`~/.physiclaw/config.toml`.

Thinking: on by default, so a call that asks for "off" must send the
switch (an omitted `think:` still gets that default). `thinking:
{type: disabled | enabled}` toggles it and `reasoning_effort: low |
high | max` scales it, both top-level body fields. Every id gets the
same table.
"""

from typing import Any

from physiclaw.contract.dto import Thinking
from physiclaw.provider.chat.base import NO_CACHE_MARKERS
from physiclaw.provider.chat.openai_compat import OpenAICompatibleProvider


class DeepSeekChatProvider(OpenAICompatibleProvider):
    PROVIDER_ID = "deepseek"
    BASE_URL = "https://api.deepseek.com/v1"
    CACHE_MARKERS = NO_CACHE_MARKERS

    def thinking_params(self, thinking: Thinking) -> dict[str, Any]:
        if thinking == "off":
            return {"thinking": {"type": "disabled"}}
        effort = {"low": "low", "medium": "high", "high": "max"}
        return {"thinking": {"type": "enabled"}, "reasoning_effort": effort[thinking]}
