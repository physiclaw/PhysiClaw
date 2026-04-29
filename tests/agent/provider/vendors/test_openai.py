"""Tests for `physiclaw.agent.provider.vendors.openai` — adapter class.

Native OpenAI endpoint. Reasoning models surface chain-of-thought via
API field, not a prompt wrapper — so unlike Qwen, no
`system_prompt_fragment` override.
"""
from __future__ import annotations

from physiclaw.agent.provider.vendors.openai import OpenAIProvider


def test_provider_id_pinned() -> None:
    """Provider id is the API surface (`openai`), matching the OpenClaw
    / Anthropic-SDK convention — not the brand (`chatgpt`)."""
    assert OpenAIProvider.PROVIDER_ID == "openai"


def test_base_url_pinned() -> None:
    assert OpenAIProvider.BASE_URL == "https://api.openai.com/v1"


def test_inherits_openai_compat() -> None:
    from physiclaw.agent.provider.openai_compat import OpenAICompatibleProvider

    assert issubclass(OpenAIProvider, OpenAICompatibleProvider)


def test_no_system_prompt_fragment_override() -> None:
    """Unlike Qwen (which wraps reasoning in `<think>...</think>`),
    OpenAI's reasoning models surface chain-of-thought via API field,
    not a prompt convention. The vendor inherits the base (no-op)
    fragment from BaseProvider rather than overriding it."""
    # Qwen overrides → 'QwenProvider.system_prompt_fragment'.
    # OpenAI inherits → 'BaseProvider.system_prompt_fragment'.
    assert (
        OpenAIProvider.system_prompt_fragment.__qualname__
        != "OpenAIProvider.system_prompt_fragment"
    )
