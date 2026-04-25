"""Google Gemini — via Google's OpenAI-compatible endpoint.

Google maintains a first-party OpenAI-compatible shim at
`/v1beta/openai/`, with native tool_calls and image inputs. We use that
rather than the bespoke `generativelanguage.googleapis.com` REST API —
the shim is well-supported and shares the same `chat()` flow as every
other vendor here.

Gemini 3 series uses dynamic thinking by default; reasoning surfaces
through the shim's `reasoning_content` field (handled in
`OpenAICompatibleProvider._parse_response`). No extra wiring needed.

Auth: `GOOGLE_API_KEY` env, or `[provider] google_api_key` in
`~/.physiclaw/config.toml`.

Model ref examples:  `google/gemini-3.1-pro-preview`,
`google/gemini-3-flash-preview`, `google/gemini-2.5-pro`.

`gemini-2.5-flash` is omitted — it CAN reason via the `thinkingBudget`
parameter, but defaults to off. Until we wire that param, it would
return non-reasoning responses, which violates PhysiClaw's requirement.
Gemini 3 series (`gemini-3.x-*-preview`) uses dynamic thinking by default.
"""
from physiclaw.agent.provider.provider_base import ModelEntry
from physiclaw.agent.provider.openai_compat import OpenAICompatibleProvider


class GoogleProvider(OpenAICompatibleProvider):
    PROVIDER_ID = "google"
    BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
    # API_KEY_ENV_VARS defaults to ("GOOGLE_API_KEY",) by convention.
    MODELS = (
        ModelEntry("gemini-3.1-pro-preview", context_window=2_000_000),
        ModelEntry("gemini-3-flash-preview", context_window=1_000_000),
        ModelEntry("gemini-2.5-pro",         context_window=2_000_000),
    )
