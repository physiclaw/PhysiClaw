"""Tests for `physiclaw.provider.vendors.openai` — declaration.

Native OpenAI endpoint. Reasoning models surface chain-of-thought via
API field, not a prompt wrapper — so unlike Qwen, no declared
`SYSTEM_PROMPT_FRAGMENT`.
"""

from __future__ import annotations

import pytest

from physiclaw.provider.vendors.openai import OpenAIProvider


@pytest.fixture(autouse=True)
def _stub_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")


def test_provider_id_pinned() -> None:
    """Provider id is the API surface (`openai`), matching the OpenClaw
    / Anthropic-SDK convention — not the brand (`chatgpt`)."""
    assert OpenAIProvider.PROVIDER_ID == "openai"


def test_base_url_pinned() -> None:
    assert OpenAIProvider.BASE_URL == "https://api.openai.com/v1"


def test_inherits_openai_compat() -> None:
    from physiclaw.provider.openai_compat import OpenAICompatibleProvider

    assert issubclass(OpenAIProvider, OpenAICompatibleProvider)


def test_no_system_prompt_fragment_declared() -> None:
    """Unlike Qwen (which wraps reasoning in `<think>...</think>`),
    OpenAI's reasoning models surface chain-of-thought via API field,
    not a prompt convention — the vendor keeps the base's empty
    declaration."""
    assert OpenAIProvider.SYSTEM_PROMPT_FRAGMENT == ""
    assert OpenAIProvider.system_prompt_fragment() == ""


# ---------- thinking table ----------


def test_gpt5_takes_minimal_for_off_and_the_o_series_floors_at_low():
    p = OpenAIProvider(model="gpt-5")
    assert p.thinking_params("off") == {"reasoning_effort": "minimal"}
    assert p.thinking_params("high") == {"reasoning_effort": "high"}
    o = OpenAIProvider(model="o4-mini")
    assert o.thinking_params("off") == {"reasoning_effort": "low"}
    assert o.thinking_params("medium") == {"reasoning_effort": "medium"}


def test_a_non_reasoning_model_gets_no_field() -> None:
    assert OpenAIProvider(model="gpt-4o").thinking_params("off") == {}
