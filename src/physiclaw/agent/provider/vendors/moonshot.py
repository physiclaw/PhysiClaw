"""Moonshot — OpenAI-compatible endpoint.

Provider id is `moonshot` (matches OpenClaw / the company name —
`MOONSHOT_API_KEY`, `api.moonshot.cn`). `kimi` is a model brand, not the
API surface.

`BASE_URL` defaults to the China endpoint (`api.moonshot.cn`). For
international keys, override per-instance with `base_url=` or use
`api.moonshot.ai` — Moonshot accepts both, but a key minted for one
domain returns 401 on the other.

Auth: `MOONSHOT_API_KEY` env, or `[provider] moonshot_api_key` in
`~/.physiclaw/config.toml`.

Model ref examples:  `moonshot/kimi-k2.6`, `moonshot/kimi-k2.5`.

`kimi-latest` is omitted — it's an unstable alias to Moonshot's general
chat lineage (text-only, doesn't satisfy PhysiClaw's vision
requirement). Use the explicit K-series ids; both are native multimodal
agentic models with vision.
"""
from physiclaw.agent.provider.provider_base import ModelEntry
from physiclaw.agent.provider.openai_compat import OpenAICompatibleProvider


class MoonshotProvider(OpenAICompatibleProvider):
    PROVIDER_ID = "moonshot"
    BASE_URL = "https://api.moonshot.cn/v1"
    # API_KEY_ENV_VARS defaults to ("MOONSHOT_API_KEY",) by convention.
    MODELS = (
        ModelEntry("kimi-k2.6", context_window=256_000),
        ModelEntry("kimi-k2.5", context_window=256_000),
    )
