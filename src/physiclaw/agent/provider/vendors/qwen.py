"""Qwen / DashScope — OpenAI-compatible endpoint.

Uses native tool_calls. The DashScope `compatible-mode` endpoint accepts
the standard OpenAI request shape; the only Qwen-specific details are
the `reasoning_content` field on responses (handled in
`base.parse_openai_response`) and the `<think>...</think>` system-prompt
fragment (declared via `THINKING_FORMAT="qwen"`).

Auth: `QWEN_API_KEY` / `DASHSCOPE_API_KEY` env, or
`[provider] qwen_api_key` in `~/.physiclaw/config.toml`.

Model ref examples:  `qwen/qwen3.6-plus`, `qwen/qwen3-max`.
"""
from physiclaw.agent.provider.provider_base import ModelEntry
from physiclaw.agent.provider.openai_compat import OpenAICompatibleProvider


class QwenProvider(OpenAICompatibleProvider):
    PROVIDER_ID = "qwen"
    BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    API_KEY_ENV_VARS = ("QWEN_API_KEY", "DASHSCOPE_API_KEY")
    THINKING_FORMAT = "qwen"
    # First entry is the implicit default when no model is passed.
    # PhysiClaw requires vision + reasoning. `qwen3-max` is text-only
    # (VL flagships live under `qwen-vl-*` / `qwen3-vl-*` ids).
    # `qwen3.6-flash` doesn't think by default (the "flash" tier).
    MODELS = (
        ModelEntry("qwen3.6-plus", context_window=1_000_000),
    )
