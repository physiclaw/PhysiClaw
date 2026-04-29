"""Tests for `physiclaw.agent.provider.vendors.moonshot` — Moonshot/K2 adapter.

The override of interest is `_parse_usage`: K2 occasionally surfaces
`cached_tokens` at the top of the `usage` block instead of nested
under `prompt_tokens_details`. The fallback only kicks in when the
nested read came up empty.
"""
from __future__ import annotations

from physiclaw.agent.engine.dto import Usage
from physiclaw.agent.provider.vendors.moonshot import MoonshotProvider


def _make_provider() -> MoonshotProvider:
    """Bypass __init__ so we don't need an API key at construction time."""
    return MoonshotProvider.__new__(MoonshotProvider)


# ---------- class metadata ----------


def test_provider_id_and_base_url_pinned() -> None:
    """Routing key + default region. Override BASE_URL via config for
    the .ai endpoint."""
    assert MoonshotProvider.PROVIDER_ID == "moonshot"
    assert MoonshotProvider.BASE_URL == "https://api.moonshot.cn/v1"


def test_inherits_openai_compat() -> None:
    from physiclaw.agent.provider.openai_compat import OpenAICompatibleProvider

    assert issubclass(MoonshotProvider, OpenAICompatibleProvider)


# ---------- _parse_usage ----------


def test_parse_usage_uses_nested_when_present() -> None:
    """Standard OpenAI shape: cached_tokens nested under
    prompt_tokens_details — base parser handles it; override is a no-op."""
    p = _make_provider()
    raw = {
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "prompt_tokens_details": {"cached_tokens": 80},
        },
    }

    usage = p._parse_usage(raw)

    assert usage.prompt_tokens == 100
    assert usage.completion_tokens == 50
    assert usage.cached_tokens == 80


def test_parse_usage_falls_through_to_top_level_when_nested_empty() -> None:
    """K2 quirk: cached_tokens lives at the top of the usage block,
    not under prompt_tokens_details. Override picks it up only when
    the nested location is empty/missing."""
    p = _make_provider()
    raw = {
        "usage": {
            "prompt_tokens": 200,
            "completion_tokens": 30,
            "cached_tokens": 150,  # top-level, no nested details
        },
    }

    usage = p._parse_usage(raw)

    assert usage.cached_tokens == 150
    assert usage.prompt_tokens == 200


def test_parse_usage_prefers_nested_over_top_level_when_both_set() -> None:
    """Defensive: if a response carries both, nested wins (it's the
    standard location). Override only fires on empty nested."""
    p = _make_provider()
    raw = {
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "cached_tokens": 999,  # would-be top-level
            "prompt_tokens_details": {"cached_tokens": 80},
        },
    }

    usage = p._parse_usage(raw)

    assert usage.cached_tokens == 80


def test_parse_usage_zero_when_neither_location_has_value() -> None:
    p = _make_provider()
    raw = {
        "usage": {"prompt_tokens": 100, "completion_tokens": 10},
    }

    usage = p._parse_usage(raw)

    assert usage.cached_tokens == 0


def test_parse_usage_handles_missing_usage_block() -> None:
    """Some error responses arrive without a usage block at all."""
    p = _make_provider()

    usage = p._parse_usage({})

    assert isinstance(usage, Usage)
    assert usage.cached_tokens == 0
    assert usage.prompt_tokens == 0


def test_parse_usage_handles_none_usage_block() -> None:
    """Defensive: usage key exists but is None."""
    p = _make_provider()

    usage = p._parse_usage({"usage": None})

    assert usage.cached_tokens == 0


def test_parse_usage_handles_string_top_level_cached_tokens() -> None:
    """Some upstream proxies stringify integers; the int() cast in the
    fallback must accept that."""
    p = _make_provider()
    raw = {
        "usage": {
            "prompt_tokens": 50,
            "completion_tokens": 5,
            "cached_tokens": "42",  # stringified
        },
    }

    usage = p._parse_usage(raw)

    assert usage.cached_tokens == 42
