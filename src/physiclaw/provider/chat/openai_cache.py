"""Explicit OpenAI cache protocol, shared by direct and routed requests.

https://developers.openai.com/api/docs/guides/prompt-caching
https://openrouter.ai/docs/guides/best-practices/prompt-caching
"""

import re

from physiclaw.provider.chat.history import CacheMarkers

_MODELS = {
    "gpt-5.6-luna",
    "gpt-5.6-terra",
    "gpt-5.6-sol",
    "gpt-6-astra",
    "gpt-6-sol",
    "gpt-6-luna",
    "gpt-6.1-sol",
    "gpt-6.1-terra",
}


def supports_explicit_cache(model: str) -> bool:
    # Known aliases and dated snapshots; leave unknown variants to API defaults.
    return re.sub(r"-\d{4}-\d{2}-\d{2}$", "", model) in _MODELS


class OpenAIPromptCacheMarkers(CacheMarkers):
    """Pin the system and two most recent superseded result boundaries.

    Explicit lookup requires the previous boundary in the new request. Retain
    it alongside the newest stub. Screens and per-turn metadata stay outside
    the writes. Existing block shapes stay identical apart from annotations.
    """

    stub_count = 2

    def mark_system(self, entry: dict) -> dict:
        field = "output" if entry.get("type") == "function_call_output" else "content"
        blocks = entry[field]
        if isinstance(blocks, str):
            blocks = [{"type": "text", "text": blocks}]
        return {
            **entry,
            field: [
                *blocks[:-1],
                {**blocks[-1], "prompt_cache_breakpoint": {"mode": "explicit"}},
            ],
        }

    def mark_stub(self, entry: dict) -> dict:
        return self.mark_system(entry)
