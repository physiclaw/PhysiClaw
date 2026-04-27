"""Provider registry — id → class mapping + lookup helpers.

Each concrete vendor declares its own `PROVIDER_ID` / `BASE_URL` /
`MODELS`; this module just wires them into a dict and exposes the
lookups (`provider_class`, `provider_endpoint`, `provider_key_status`,
`make_provider`). Kept separate from `__init__.py` so the package
surface stays a thin re-export layer.
"""
from physiclaw.agent.provider.provider_base import BaseProvider, Provider
from physiclaw.agent.provider.vendors.anthropic import AnthropicProvider
from physiclaw.agent.provider.vendors.deepseek import DeepSeekProvider
from physiclaw.agent.provider.vendors.google import GoogleProvider
from physiclaw.agent.provider.vendors.moonshot import MoonshotProvider
from physiclaw.agent.provider.vendors.openai import OpenAIProvider
from physiclaw.agent.provider.vendors.qwen import QwenProvider


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
    DeepSeekProvider.PROVIDER_ID:  DeepSeekProvider,
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


def provider_key_status(provider_id: str) -> tuple[str | None, str | None]:
    """Return `(masked_value_or_None, source_or_None)` for a provider's
    API key, mirroring `BaseProvider._api_key()` resolution exactly so
    CLI displays match what the runtime will pick up.

    `source` is a human-readable string like `"OPENAI_API_KEY env"` or
    `"config.toml [provider] qwen_api_key"`. Both fields are `None` if
    the key is unset OR `provider_id` isn't a known in-process provider.
    """
    from physiclaw.config import resolve_provider_key
    cls = _PROVIDER_CLASSES.get(provider_id)
    if cls is None:
        return None, None
    env_vars = cls.API_KEY_ENV_VARS or (f"{cls.PROVIDER_ID.upper()}_API_KEY",)
    val, source = resolve_provider_key(env_vars, f"{cls.PROVIDER_ID}_api_key")
    return ("********" if val else None), source


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
