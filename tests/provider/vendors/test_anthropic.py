"""Tests for `physiclaw.provider.vendors.anthropic.chat` — adapter class.

This vendor calls Anthropic's
`/v1/messages` endpoint via the `AsyncAnthropic` SDK; wire-format flow
lives in `provider/chat/anthropic_compat.py` and is exercised separately.
"""

from __future__ import annotations

import json

import httpx
import pytest

from physiclaw.contract.dto import UserMessage
from physiclaw.provider.vendors.anthropic.chat import (
    AnthropicChatProvider as AnthropicProvider,
)


def test_provider_id_pinned() -> None:
    """Routing key for the registry. Renaming breaks `physiclaw models
    use anthropic/<id>` and any config references."""
    assert AnthropicProvider.PROVIDER_ID == "anthropic"


def test_base_url_pinned_for_documentation() -> None:
    """The SDK manages the URL itself — this constant is documentation
    so operators can spot which endpoint family the provider hits."""
    assert AnthropicProvider.BASE_URL == "https://api.anthropic.com/v1"


def test_inherits_anthropic_compat() -> None:
    """Wire format / response parsing comes from the parent."""
    from physiclaw.provider.chat.anthropic_compat import (
        AnthropicCompatibleProvider,
    )

    assert issubclass(AnthropicProvider, AnthropicCompatibleProvider)


@pytest.fixture
def reply_route(respx_mock, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    return respx_mock.post("https://api.anthropic.test/v1/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "reply",
                "type": "message",
                "role": "assistant",
                "model": "test",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )
    )


_ADAPTIVE_CASES = [
    ("claude-opus-4-6", "disabled"),
    ("claude-sonnet-4-6", "disabled"),
    ("claude-opus-4-7", "disabled"),
    ("claude-opus-4-8", "disabled"),
    ("claude-opus-5", "disabled"),
    ("claude-sonnet-5", "disabled"),
    ("claude-haiku-5-5", "disabled"),
    ("claude-sonnet-5-5", "between_tools"),
    ("claude-opus-5-5", "adaptive"),
    ("claude-fable-5", "adaptive"),
    ("claude-fable-5-1", "adaptive"),
    ("claude-mythos-5", "adaptive"),
    ("claude-mythos-5-1", "adaptive"),
    ("claude-mythos-preview", "adaptive"),
    ("claude-opus-4-7-20260416", "disabled"),
]


@pytest.mark.parametrize("model, off_mode", _ADAPTIVE_CASES)
async def test_chat_off_uses_lowest_supported_mode(model, off_mode, reply_route):
    provider = AnthropicProvider(model=model, base_url="https://api.anthropic.test")

    try:
        await provider.chat([UserMessage(content="hi")], [], thinking="off")
    finally:
        await provider.aclose()

    body = json.loads(reply_route.calls.last.request.content)
    assert body["thinking"] == {"type": off_mode}
    assert body["output_config"] == {"effort": "low"}
    assert body["max_tokens"] == 8192


@pytest.mark.parametrize(
    "thinking, max_tokens", [("low", 9216), ("medium", 12288), ("high", 24576)]
)
@pytest.mark.parametrize("model, _off_mode", _ADAPTIVE_CASES)
async def test_chat_adaptive_effort_preserves_output_allowance(
    model, _off_mode, thinking, max_tokens, reply_route
):
    provider = AnthropicProvider(model=model, base_url="https://api.anthropic.test")

    try:
        await provider.chat([UserMessage(content="hi")], [], thinking=thinking)
    finally:
        await provider.aclose()

    body = json.loads(reply_route.calls.last.request.content)
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"effort": thinking}
    assert body["max_tokens"] == max_tokens


@pytest.mark.parametrize(
    "model", ["claude-haiku-4-5-20251001", "claude-opus-4-5", "custom-model"]
)
async def test_chat_preserves_manual_budget_for_older_or_custom_models(
    model, reply_route
):
    provider = AnthropicProvider(model=model, base_url="https://api.anthropic.test")

    try:
        await provider.chat([UserMessage(content="hi")], [], thinking="low")
    finally:
        await provider.aclose()

    body = json.loads(reply_route.calls.last.request.content)
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 1024}
    assert body["max_tokens"] == 9216
    assert "output_config" not in body


async def test_chat_without_thinking_keeps_model_default(reply_route):
    provider = AnthropicProvider(
        model="claude-opus-5-5", base_url="https://api.anthropic.test"
    )

    try:
        await provider.chat([UserMessage(content="hi")], [])
    finally:
        await provider.aclose()

    body = json.loads(reply_route.calls.last.request.content)
    assert "thinking" not in body
    assert "output_config" not in body
