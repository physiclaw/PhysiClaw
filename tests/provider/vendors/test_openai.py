"""Tests for `physiclaw.provider.vendors.openai.chat` — declaration.

Native OpenAI endpoint. Reasoning models surface chain-of-thought via
API field, not a prompt wrapper — so unlike Qwen, no declared
`SYSTEM_PROMPT_FRAGMENT`.
"""

from __future__ import annotations

import json

import httpx
import pytest

from physiclaw.contract.dto import UserMessage
from physiclaw.provider.vendors.openai.chat import OpenAIChatProvider


@pytest.fixture(autouse=True)
def _stub_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")


def test_provider_id_pinned() -> None:
    """Provider id is the API surface (`openai`), matching the OpenClaw
    / Anthropic-SDK convention — not the brand (`chatgpt`)."""
    assert OpenAIChatProvider.PROVIDER_ID == "openai"


def test_base_url_pinned() -> None:
    assert OpenAIChatProvider.BASE_URL == "https://api.openai.com/v1"


def test_inherits_openai_compat() -> None:
    from physiclaw.provider.chat.openai_compat import OpenAICompatibleProvider

    assert issubclass(OpenAIChatProvider, OpenAICompatibleProvider)


def test_no_system_prompt_fragment_declared() -> None:
    """Unlike Qwen (which wraps reasoning in `<think>...</think>`),
    OpenAI's reasoning models surface chain-of-thought via API field,
    not a prompt convention — the vendor keeps the base's empty
    declaration."""
    assert OpenAIChatProvider.SYSTEM_PROMPT_FRAGMENT == ""
    assert OpenAIChatProvider.system_prompt_fragment() == ""


# ---------- thinking table ----------


def test_gpt5_takes_minimal_for_off_and_the_o_series_floors_at_low():
    p = OpenAIChatProvider(model="gpt-5")
    assert p.thinking_params("off") == {"reasoning_effort": "minimal"}
    assert p.thinking_params("high") == {"reasoning_effort": "high"}
    o = OpenAIChatProvider(model="o4-mini")
    assert o.thinking_params("off") == {"reasoning_effort": "low"}
    assert o.thinking_params("medium") == {"reasoning_effort": "medium"}


def test_a_non_reasoning_model_gets_no_field() -> None:
    assert OpenAIChatProvider(model="gpt-4o").thinking_params("off") == {}


@pytest.mark.parametrize(
    "model, effort",
    [
        ("gpt-5", "minimal"),
        ("gpt-5-mini-2025-08-07", "minimal"),
        ("gpt-5.1", "none"),
        ("gpt-5.1-2025-11-13", "none"),
        ("gpt-5.2", "none"),
        ("gpt-5.4", "none"),
        ("gpt-5.5", "none"),
        ("gpt-6-luna", "none"),
    ],
)
@pytest.mark.asyncio
async def test_chat_off_sends_effort_supported_by_model(model, effort, respx_mock):
    route = respx_mock.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        )
    )
    provider = OpenAIChatProvider(model=model)

    try:
        await provider.chat(
            [UserMessage(content="hi")],
            [{"name": "probe", "input_schema": {"type": "object", "properties": {}}}],
            thinking="off",
        )
    finally:
        await provider.aclose()

    body = json.loads(route.calls.last.request.content)
    assert body["reasoning_effort"] == effort
    assert body["tools"][0]["function"]["name"] == "probe"


@pytest.mark.parametrize(
    "model", ["gpt-5.1-chat-latest", "gpt-5.2-pro", "gpt-5.1-codex", "gpt-5.99"]
)
def test_unknown_variant_keeps_its_api_default(model):
    provider = OpenAIChatProvider(model=model)

    params = provider.thinking_params("off")

    assert params == {}
