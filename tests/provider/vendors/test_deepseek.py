"""Tests for `physiclaw.provider.vendors.deepseek.chat` — DeepSeek (V4.1
Flash) declaration.

Pure pins plus the declared knobs: cache markers OFF and the thinking
table — rationale in `vendors/deepseek.py`. Usage parsing rides the
base `_parse_usage` fallback, exercised in `test_openai_compat.py`.
"""

from __future__ import annotations

import pytest

from physiclaw.provider.chat.base import NO_CACHE_MARKERS
from physiclaw.provider.chat.openai_compat import OpenAICompatibleProvider
from physiclaw.provider.vendors.deepseek.chat import (
    DeepSeekChatProvider as DeepSeekProvider,
)


@pytest.fixture(autouse=True)
def _stub_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")


# ---------- class metadata ----------


def test_provider_id_and_base_url_pinned() -> None:
    """The provider id and base URL are part of the registry key —
    rename them and routing breaks silently elsewhere."""
    assert DeepSeekProvider.PROVIDER_ID == "deepseek"
    assert DeepSeekProvider.BASE_URL == "https://api.deepseek.com/v1"


def test_inherits_openai_compat() -> None:
    assert issubclass(DeepSeekProvider, OpenAICompatibleProvider)


def test_constructs_with_key_set() -> None:
    """No longer a stub — with a credential present, construction
    succeeds. The model id passes through verbatim."""

    p = DeepSeekProvider(model="deepseek-flash")

    assert p.model == "deepseek-flash"


# ---------- declared knobs ----------


def test_cache_markers_disabled() -> None:
    """DeepSeek's prefix cache is automatic and marker-free — see
    `vendors/deepseek.py`. (The null-object template path itself is
    pinned in `test_provider_base.py`.)"""
    assert DeepSeekProvider.CACHE_MARKERS is NO_CACHE_MARKERS


def test_no_parse_usage_override() -> None:
    """DeepSeek's `prompt_cache_hit_tokens` spelling is handled by the
    base `_parse_usage` fallback (exercised in `test_openai_compat.py`)
    — a vendor override reappearing here would mean the quirk handling
    is duplicated."""
    assert "_parse_usage" not in DeepSeekProvider.__dict__


# ---------- thinking table ----------


@pytest.mark.parametrize(
    "level, effort", [("low", "low"), ("medium", "high"), ("high", "max")]
)
def test_the_levels_scale_reasoning_effort(level, effort) -> None:
    assert DeepSeekProvider(model="deepseek-flash").thinking_params(level) == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": effort,
    }


@pytest.mark.parametrize(
    "model",
    [
        "deepseek-flash",
        "deepseek-v4-pro",
        "deepseek-v4-flash-vision-exp",
        "deepseek-chat",
    ],
)
def test_off_sends_the_switch_for_every_id(model) -> None:
    """The model thinks by default, the retired ids are served by Flash
    and Pro has the same switch: one table, no id left thinking at full
    effort by omission."""
    assert DeepSeekProvider(model=model).thinking_params("off") == {
        "thinking": {"type": "disabled"}
    }
