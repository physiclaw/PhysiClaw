"""Tests for `physiclaw.provider.vendors.qwen.chat` — DashScope
declaration.

Pins cover routing, authentication precedence, and provider thinking options.
"""

from __future__ import annotations

import pytest

from physiclaw.provider.vendors.qwen.chat import QwenChatProvider


@pytest.fixture(autouse=True)
def _stub_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QWEN_API_KEY", "sk-test")


# ---------- class metadata ----------


def test_provider_id_pinned() -> None:
    assert QwenChatProvider.PROVIDER_ID == "qwen"


def test_base_url_pinned() -> None:
    """DashScope's OpenAI-compatible endpoint."""
    assert QwenChatProvider.BASE_URL == (
        "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )


def test_inherits_openai_compat() -> None:
    from physiclaw.provider.chat.openai_compat import OpenAICompatibleProvider

    assert issubclass(QwenChatProvider, OpenAICompatibleProvider)


def test_api_key_env_vars_supports_both_qwen_and_dashscope() -> None:
    """Auth precedence — QWEN_API_KEY first, DASHSCOPE_API_KEY as alias."""
    assert QwenChatProvider.API_KEY_ENV_VARS == ("QWEN_API_KEY", "DASHSCOPE_API_KEY")


# ---------- thinking table ----------


def test_hybrid_models_switch_and_bound_thinking() -> None:
    p = QwenChatProvider(model="qwen3-max")
    assert p.thinking_params("off") == {"enable_thinking": False}
    assert p.thinking_params("low") == {
        "enable_thinking": True,
        "thinking_budget": 1024,
    }
    assert p.thinking_params("high") == {
        "enable_thinking": True,
        "thinking_budget": 16384,
    }


def test_a_model_without_the_switch_gets_no_field() -> None:
    assert QwenChatProvider(model="qwen-max").thinking_params("off") == {}
