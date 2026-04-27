"""DeepSeek — OpenAI-compatible endpoint. Stub: vision not supported.

DeepSeek's chat-completions API accepts text-only `content` (no
`image_url` part per the official API reference). PhysiClaw requires
vision on every `peek`, so the provider can't drive the loop.

Auth: `DEEPSEEK_API_KEY` env, or `[provider] deepseek_api_key` in
`~/.physiclaw/config.toml`.

Model ref examples: `deepseek/deepseek-reasoner`, `deepseek/deepseek-chat`.
"""
from physiclaw.agent.provider.openai_compat import OpenAICompatibleProvider
from physiclaw.agent.provider.provider_base import ModelEntry


class DeepSeekProvider(OpenAICompatibleProvider):
    PROVIDER_ID = "deepseek"
    BASE_URL = "https://api.deepseek.com/v1"
    # API_KEY_ENV_VARS defaults to ("DEEPSEEK_API_KEY",) by convention.
    MODELS = (
        ModelEntry("deepseek-reasoner", context_window=128_000, vision=False),
        ModelEntry("deepseek-chat",     context_window=128_000, vision=False),
    )

    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "deepseek provider is not implemented — chat endpoint is "
            "text-only and PhysiClaw requires vision for every peek."
        )
