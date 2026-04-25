"""Anthropic — Claude models via the official `anthropic` SDK.

Distinct from `claude-code` (the subprocess engine that shells out to
the `claude` CLI). This provider calls Anthropic's `/v1/messages`
endpoint directly through `AsyncAnthropic` — wire-format flow lives in
`provider/anthropic_compat.py`; this file just declares the catalog.

Auth: `ANTHROPIC_API_KEY` env, or `[provider] anthropic_api_key` in
`~/.physiclaw/config.toml`.

Model ref examples:  `anthropic/claude-opus-4-7`,
`anthropic/claude-sonnet-4-6`, `anthropic/claude-haiku-4-5`.
"""
from physiclaw.agent.provider.anthropic_compat import AnthropicCompatibleProvider
from physiclaw.agent.provider.provider_base import ModelEntry


class AnthropicProvider(AnthropicCompatibleProvider):
    PROVIDER_ID = "anthropic"
    # Informational only — `AsyncAnthropic` manages the URL itself.
    BASE_URL = "https://api.anthropic.com/v1"
    # API_KEY_ENV_VARS defaults to ("ANTHROPIC_API_KEY",) by convention.
    THINKING_FORMAT = None
    # All Claude 4.x support extended thinking + vision.
    MODELS = (
        ModelEntry("claude-opus-4-7",   context_window=200_000),
        ModelEntry("claude-sonnet-4-6", context_window=200_000),
        ModelEntry("claude-haiku-4-5",  context_window=200_000),
    )
