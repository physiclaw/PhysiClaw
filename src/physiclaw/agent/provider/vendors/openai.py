"""OpenAI — native OpenAI endpoint.

Provider id is `openai` (matches OpenClaw / Anthropic-SDK convention —
the API surface, not the brand `chatgpt`). Reasoning models (gpt-5,
o4-mini) surface chain-of-thought via API field, not a prompt wrapper —
no `THINKING_FORMAT` needed.

Auth: `OPENAI_API_KEY` env, or `[provider] openai_api_key` in
`~/.physiclaw/config.toml`.

Model ref examples:  `openai/gpt-5.4`, `openai/gpt-5.4-mini`.
"""
from physiclaw.agent.provider.provider_base import ModelEntry
from physiclaw.agent.provider.openai_compat import OpenAICompatibleProvider


class OpenAIProvider(OpenAICompatibleProvider):
    PROVIDER_ID = "openai"
    BASE_URL = "https://api.openai.com/v1"
    # API_KEY_ENV_VARS defaults to ("OPENAI_API_KEY",) by convention.
    # gpt-4o is omitted — vision but no reasoning channel (pre-o-series).
    MODELS = (
        ModelEntry("gpt-5.4",      context_window=400_000),
        ModelEntry("gpt-5.4-mini", context_window=400_000),
    )
