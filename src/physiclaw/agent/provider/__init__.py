"""Provider package — one file per vendor, two wire-shape bases.

Provider ids follow OpenClaw's convention: the API surface (vendor /
company), not the brand. So `openai` (not `chatgpt`), `moonshot` (not
`kimi`), `google` (not `gemini`), `qwen` (Alibaba's Qwen API),
`anthropic` (direct API). The existing `claude-code` is a separate
concept (subprocess engine, not direct Anthropic API).

Layout:
  - `provider_base.py`     — `Provider` Protocol, `ModelEntry`, errors,
                             `BaseProvider` (catalog + auth +
                             system-prompt fragment hooks +
                             `serialize_history` template)
  - `openai_compat.py`     — `OpenAICompatibleProvider`:
                             `/chat/completions` wire flow + cache
                             markers + response parser
  - `anthropic_compat.py`  — `AnthropicCompatibleProvider`:
                             `/v1/messages` wire flow (Anthropic SDK) +
                             per-block cache markers
  - `wire.py`              — DTO ↔ OpenAI wire-format adapters used by
                             `openai_compat.py`
  - `vendors/`             — one file per concrete vendor:
      - `qwen.py`      — `QwenProvider` (DashScope)   — OpenAI-compat
      - `moonshot.py`  — `MoonshotProvider` (Kimi)    — OpenAI-compat
      - `openai.py`    — `OpenAIProvider` (GPT-5)     — OpenAI-compat
      - `google.py`    — `GoogleProvider` (Gemini)    — OpenAI-compat
      - `anthropic.py` — `AnthropicProvider` (Claude) — Anthropic-compat

Selection: the user picks a `provider/model` ref (e.g. `qwen/qwen3.6-plus`)
in `~/.physiclaw/config.toml [agent] model`. The launcher parses the ref
and routes on the provider id — `claude-code` goes to the subprocess
engine, anything else here goes to the in-process engine via
`make_provider(provider_id, model_id)`.
"""
from physiclaw.agent.provider.anthropic_compat import AnthropicCompatibleProvider
from physiclaw.agent.provider.provider_base import (
    BaseProvider,
    ModelEntry,
    Provider,
    ProviderError,
    ProviderPermanentError,
    ProviderTransientError,
)
from physiclaw.agent.provider.openai_compat import OpenAICompatibleProvider
from physiclaw.agent.provider.vendors.anthropic import AnthropicProvider
from physiclaw.agent.provider.vendors.google import GoogleProvider
from physiclaw.agent.provider.vendors.moonshot import MoonshotProvider
from physiclaw.agent.provider.vendors.openai import OpenAIProvider
from physiclaw.agent.provider.vendors.qwen import QwenProvider
from physiclaw.agent.provider.wire import (
    assistant_to_wire,
    mcp_blocks_to_content_blocks,
    tool_result_to_wire,
    tool_to_wire,
    user_content_to_openai,
)


# Special provider id — the claude-code subprocess engine. Not in
# `_PROVIDER_CLASSES` because it isn't an in-process Provider; the
# launcher recognizes this id and routes to the subprocess path.
CLAUDE_CODE_ID = "claude-code"


# Single source of truth: each provider class declares its own
# PROVIDER_ID / BASE_URL / MODELS. The registry just maps id → class.
# Typed against `BaseProvider` so both wire-shape lineages fit.
_PROVIDER_CLASSES: dict[str, type[BaseProvider]] = {
    QwenProvider.PROVIDER_ID:      QwenProvider,
    MoonshotProvider.PROVIDER_ID:  MoonshotProvider,
    OpenAIProvider.PROVIDER_ID:    OpenAIProvider,
    AnthropicProvider.PROVIDER_ID: AnthropicProvider,
    GoogleProvider.PROVIDER_ID:    GoogleProvider,
}


def in_process_provider_ids() -> tuple[str, ...]:
    """Provider ids handled by the in-process engine (every class in this
    package). Stubs are included — they fail at instantiation with a
    clear message rather than silently disappearing from the menu."""
    return tuple(_PROVIDER_CLASSES)


def provider_class(provider_id: str) -> type[BaseProvider] | None:
    """Class for an in-process provider id, or None if `provider_id`
    isn't one of ours (e.g. `claude-code`)."""
    return _PROVIDER_CLASSES.get(provider_id)


def provider_endpoint(provider_id: str) -> tuple[str, str]:
    """Return `(base_url, default_model_id)` for an in-process provider.

    Used by `doctor` to probe the endpoint without instantiating the
    provider (which would require an API key). Reads `MODELS[0]` as the
    probe model.

    Raises `KeyError` if `provider_id` isn't an in-process provider.
    """
    cls = _PROVIDER_CLASSES.get(provider_id)
    if cls is None:
        raise KeyError(
            f"no in-process provider class for {provider_id!r}; "
            f"known: {tuple(_PROVIDER_CLASSES)}"
        )
    return (cls.BASE_URL, cls.default_model().id)


def make_provider(provider_id: str, model_id: str) -> Provider:
    """`(provider_id, model_id)` → `Provider` instance.

    Validates `model_id` against the provider's catalog (`MODELS`); raises
    `ValueError` with the known set if it's not listed. Credentials come
    from the provider's own `_api_key()` lookup in `__init__`; stubs raise
    `NotImplementedError` from there with a setup pointer.
    """
    cls = _PROVIDER_CLASSES.get(provider_id)
    if cls is None:
        raise ValueError(
            f"unknown provider {provider_id!r} "
            f"(known in-process: {', '.join(_PROVIDER_CLASSES)}; "
            f"or use {CLAUDE_CODE_ID!r} for the subprocess engine)"
        )
    if not cls.has_model(model_id):
        known = ", ".join(m.id for m in cls.MODELS)
        raise ValueError(
            f"model {model_id!r} not in {provider_id} catalog (known: {known}); "
            f"declare it in agent/provider/vendors/{provider_id}.py to add"
        )
    entry = cls.find_model(model_id)
    if entry is not None:
        missing = [
            name for name, ok in (("vision", entry.vision), ("reasoning", entry.reasoning))
            if not ok
        ]
        if missing:
            raise ValueError(
                f"model {provider_id}/{model_id} lacks: {', '.join(missing)}. "
                "PhysiClaw requires both — vision because every peek ships a "
                "camera frame, reasoning because the agent loop's quality "
                "depends on thinking before tool_calls. Pick another entry "
                "from `physiclaw models list`."
            )
    return cls(model=model_id)


__all__ = [
    "CLAUDE_CODE_ID",
    "AnthropicCompatibleProvider",
    "AnthropicProvider",
    "BaseProvider",
    "GoogleProvider",
    "ModelEntry",
    "MoonshotProvider",
    "OpenAICompatibleProvider",
    "OpenAIProvider",
    "Provider",
    "ProviderError",
    "ProviderPermanentError",
    "ProviderTransientError",
    "QwenProvider",
    "assistant_to_wire",
    "in_process_provider_ids",
    "make_provider",
    "mcp_blocks_to_content_blocks",
    "provider_class",
    "provider_endpoint",
    "tool_result_to_wire",
    "tool_to_wire",
    "user_content_to_openai",
]
