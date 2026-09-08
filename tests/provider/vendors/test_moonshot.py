"""Tests for `physiclaw.provider.vendors.moonshot` — Moonshot/K2
declaration.

The vendor is a pure declaration: K2's top-level `usage.cached_tokens`
quirk is handled by the base `_parse_usage` fallback (exercised in
`test_openai_compat.py`), and the load-bearing cache markers are the
inherited `OpenAICacheMarkers`.
"""

from __future__ import annotations

import pytest

from physiclaw.provider.openai_compat import OpenAICompatibleProvider
from physiclaw.provider.provider_base import NO_CACHE_MARKERS
from physiclaw.provider.vendors.moonshot import MoonshotProvider


@pytest.fixture(autouse=True)
def _stub_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-test")


# ---------- class metadata ----------


def test_provider_id_and_base_url_pinned() -> None:
    """Routing key + default region. Override BASE_URL via config for
    the .ai endpoint."""
    assert MoonshotProvider.PROVIDER_ID == "moonshot"
    assert MoonshotProvider.BASE_URL == "https://api.moonshot.cn/v1"


def test_inherits_openai_compat() -> None:
    assert issubclass(MoonshotProvider, OpenAICompatibleProvider)


# ---------- inherited cache markers ----------


def test_inherits_cache_markers() -> None:
    """The inherited `OpenAICacheMarkers` are load-bearing for K2
    cross-wake cache hits (see the module docstring's A/B results) —
    a well-meaning `CACHE_MARKERS = NO_CACHE_MARKERS` here would
    silently cold-start every wake."""
    assert MoonshotProvider.CACHE_MARKERS is OpenAICompatibleProvider.CACHE_MARKERS
    assert MoonshotProvider.CACHE_MARKERS is not NO_CACHE_MARKERS


def test_no_parse_usage_override() -> None:
    """K2's top-level `cached_tokens` quirk is handled by the base
    `_parse_usage` fallback — a vendor override reappearing here would
    mean the quirk handling is duplicated."""
    assert "_parse_usage" not in MoonshotProvider.__dict__


# ---------- thinking table ----------


@pytest.mark.parametrize(
    "level, switch",
    [
        ("off", "disabled"),
        ("low", "disabled"),
        ("medium", "enabled"),
        ("high", "enabled"),
    ],
)
def test_k2_has_one_switch_so_the_levels_split_in_two(level, switch):
    assert MoonshotProvider(model="kimi-k2.6").thinking_params(level) == {
        "thinking": {"type": switch}
    }


@pytest.mark.parametrize(
    "level, effort",
    [("off", "low"), ("low", "low"), ("medium", "high"), ("high", "max")],
)
def test_k3_scales_reasoning_effort_never_the_thinking_block(
    monkeypatch, level, effort
):
    assert MoonshotProvider(model="kimi-k3").thinking_params(level) == {
        "reasoning_effort": effort
    }


def test_a_model_outside_the_table_sends_nothing() -> None:
    assert MoonshotProvider(model="moonshot-v1-8k").thinking_params("off") == {}
