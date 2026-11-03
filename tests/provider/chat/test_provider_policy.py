"""Real request construction and SDK transport behavior, without external calls."""

import asyncio
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace

import pytest
from freezegun import freeze_time

from physiclaw.common.provider_errors import (
    ProviderConfigurationError,
    ProviderPermanentError,
    ProviderTransientError,
)
from physiclaw.contract.dto import UserMessage
from physiclaw.contract.structured import OutputSpec
from physiclaw.provider import provider_class
from physiclaw.provider.chat.retry import retry_after_seconds


@pytest.mark.parametrize(
    "code",
    [
        "insufficient_quota",
        "credit_balance_exhausted",
        "organization_spend_limit_exceeded",
        "project_spend_limit_exceeded",
        "organization_usage_limit_exceeded",
        None,
    ],
)
@pytest.mark.parametrize("api", ["chat", "models", "decision"])
async def test_exhausted_quota_is_permanent_not_a_rate_limit(
    code, api, monkeypatch, respx_mock
):
    import httpx

    from physiclaw.provider.decision.wire import check_response_status

    payload = {
        "error": {
            "type": "insufficient_quota" if code is None else "billing_error",
            "code": code,
            "message": "No credits remaining",
        }
    }
    if api == "decision":
        with pytest.raises(ProviderPermanentError) as caught:
            check_response_status(httpx.Response(429, json=payload), vendor="OpenAI")
        assert caught.value.status_code == 429
        assert "No credits" not in str(caught.value)
        return
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    provider = provider_class("openai")(model="gpt-6.1-terra")
    route = respx_mock.route().respond(429, json=payload, headers={"Retry-After": "1"})
    try:
        with pytest.raises(ProviderPermanentError) as caught:
            if api == "chat":
                await provider.chat([UserMessage("test")], [])
            else:
                await provider.list_models()
        assert caught.value.status_code == 429
        assert caught.value.retry_after is None
        assert route.call_count == 1
    finally:
        await provider.aclose()


@pytest.mark.parametrize(
    "payload",
    [
        {"error": {"type": "rate_limit_error", "code": "slow_down"}},
        {"error": {"code": ["insufficient_quota"]}},
        {"error": None},
        [],
    ],
)
async def test_temporary_or_malformed_429_keeps_rate_limit_retry_hint(
    payload, monkeypatch, respx_mock
):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    provider = provider_class("openai")(model="gpt-6.1-terra")
    route = respx_mock.route().respond(429, json=payload, headers={"Retry-After": "7"})
    try:
        with pytest.raises(ProviderTransientError) as caught:
            await provider.chat([UserMessage("test")], [])
        assert caught.value.retry_after == 7
        assert route.call_count == 1
    finally:
        await provider.aclose()


@pytest.mark.parametrize("vendor", ["deepseek", "anthropic"])
@pytest.mark.parametrize(
    "malformation", ["empty_stop", "duplicate_call_id", "missing_tool_name"]
)
@pytest.mark.parametrize("structured", [False, True])
async def test_invalid_completion_preserves_paid_usage_without_partial_calls(
    vendor, malformation, structured, spec, monkeypatch, respx_mock
):
    from physiclaw.common.provider_errors import ProviderTransientError
    from tests.support.provider_responses import response

    monkeypatch.setenv(f"{vendor.upper()}_API_KEY", "test")
    provider = provider_class(vendor)(model="fixture")
    events = []

    provider.usage_sink = SimpleNamespace(write=events.append)
    raw = response(vendor, {"ok": True}, 0)
    if vendor == "anthropic":
        calls = [raw["content"][-1]]
        if malformation == "empty_stop":
            raw["content"] = []
            raw["stop_reason"] = "end_turn"
        elif malformation == "duplicate_call_id":
            raw["content"] = [calls[0], deepcopy(calls[0])]
        else:
            calls[0]["name"] = ""
    else:
        message = raw["choices"][0]["message"]
        if malformation == "empty_stop":
            message["tool_calls"] = []
            raw["choices"][0]["finish_reason"] = "stop"
        elif malformation == "duplicate_call_id":
            message["tool_calls"].append(deepcopy(message["tool_calls"][0]))
        else:
            message["tool_calls"][0]["function"]["name"] = ""
    route = respx_mock.route(method="POST").respond(json=raw)
    try:
        if structured:
            completion = await provider.generate_structured([UserMessage("test")], spec)
            assert completion.failure.kind == "format"
            assert completion.reply.tool_calls == []
        else:
            with pytest.raises(ProviderTransientError) as caught:
                await provider.chat([UserMessage("test")], [])
            assert caught.value.reply.tool_calls == []
        assert route.call_count == 1
        assert len(events) == 1
        assert events[0]["input"] == 10
        assert events[0]["output"] == 20
        assert events[0]["error"] == (
            "format" if structured else "ProviderTransientError"
        )
    finally:
        await provider.aclose()


@pytest.fixture
def spec():
    return OutputSpec(
        "sample",
        {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
        dict,
    )


@pytest.mark.parametrize(
    "model",
    [
        "claude-opus-5-5",
        "claude-sonnet-5-5",
        "claude-fable-5-1",
        "claude-mythos-5-1-20261001",
    ],
)
@pytest.mark.parametrize("thinking", [None, "off", "high"])
async def test_claude_mandatory_auto_choice(model, thinking, spec, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    provider = provider_class("anthropic")(model=model)
    try:
        body = provider.build_request(
            [UserMessage("test")], [], output_spec=spec, thinking=thinking
        )
        assert body["tool_choice"] == {
            "type": "auto",
            "disable_parallel_tool_use": True,
        }
        assert "strict" not in body["tools"][0]
        assert body["tools"][0]["input_schema"] == spec.tool()["input_schema"]
    finally:
        await provider.aclose()


@pytest.mark.parametrize(
    "model,thinking,choice",
    [
        ("claude-sonnet-4-6", None, "tool"),
        ("claude-opus-4-8", None, "tool"),
        ("claude-sonnet-4-6", "high", "auto"),
        ("claude-sonnet-4-6", "off", "tool"),
        ("claude-sonnet-5", None, "auto"),
        ("claude-sonnet-5", "off", "tool"),
        ("claude-mythos-preview", None, "auto"),
    ],
)
async def test_claude_default_thinking_is_model_specific(
    model, thinking, choice, spec, monkeypatch
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    provider = provider_class("anthropic")(model=model)
    try:
        body = provider.build_request([], [], output_spec=spec, thinking=thinking)
        assert body["tool_choice"]["type"] == choice
        if thinking is None:
            assert "thinking" not in body
    finally:
        await provider.aclose()


@pytest.mark.parametrize(
    "supported,strict,parallel",
    [
        (None, False, None),
        ("structured_outputs", False, None),
        ([{"structured_outputs": True}], False, None),
        (["tools"], False, None),
        (["tools", "structured_outputs", "parallel_tool_calls"], True, False),
    ],
)
async def test_openrouter_only_uses_advertised_capabilities(
    supported, strict, parallel, spec, monkeypatch
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    monkeypatch.setattr(
        "physiclaw.provider.vendors.openrouter.chat.discovered.load",
        lambda _: [{"id": "test/model", "supported_parameters": supported}],
    )
    provider = provider_class("openrouter")(model="test/model")
    try:
        body = provider.build_request([], [], output_spec=spec)
        assert body["tools"][0]["function"].get("strict", False) is strict
        assert body.get("parallel_tool_calls") is parallel
    finally:
        await provider.aclose()


async def test_openrouter_known_non_tool_model_fails_before_http(
    spec, monkeypatch, respx_mock
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    provider = provider_class("openrouter")(model="test/model")
    provider._catalog = {"test/model": {"supported_parameters": []}}
    try:
        with pytest.raises(ProviderConfigurationError, match="tool support"):
            await provider.generate_structured([], spec, thinking="high")
        assert not respx_mock.calls
    finally:
        await provider.aclose()


@pytest.mark.parametrize(
    "model,thinking",
    [
        ("gpt-6-astra", None),
        ("gpt-6.1-sol-2026-10-01", "off"),
        ("gpt-6.1-terra", "off"),
        ("gpt-6-luna", None),
        ("gpt-6-sol", "high"),
    ],
)
async def test_openai_responses_supports_native_output_and_reasoning_tools(
    model, thinking, spec, monkeypatch, respx_mock
):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    provider = provider_class("openai")(model=model)
    try:
        body = provider.build_request(
            [UserMessage("test")], [], output_spec=spec, thinking=thinking
        )
        assert body["text"]["format"]["strict"] is True
        assert "tools" not in body and "tool_choice" not in body
        tool_body = provider.build_request(
            [UserMessage("test")], [spec.tool()], thinking=thinking
        )
        assert tool_body["tools"][0]["name"] == spec.function_name
        assert tool_body["tools"][0]["strict"] is False
        assert not respx_mock.calls
    finally:
        await provider.aclose()


@pytest.mark.parametrize(
    "vendor,model,thinking,choice,parallel",
    [
        ("deepseek", "deepseek-flash", None, "auto", None),
        ("deepseek", "deepseek-flash", "off", "submit_turn", None),
        ("google", "gemini-3-flash-preview", None, "submit_turn", None),
        ("google", "gemini-3-flash-preview", "high", "submit_turn", None),
        ("qwen", "qwen3.6-plus", "high", "auto", False),
        ("qwen", "qwen3.6-plus", "off", "submit_turn", False),
    ],
)
async def test_vendor_tool_constraints(
    vendor, model, thinking, choice, parallel, spec, monkeypatch
):
    monkeypatch.setenv(f"{vendor.upper()}_API_KEY", "test")
    provider = provider_class(vendor)(model=model)
    try:
        body = provider.build_request([], [], output_spec=spec, thinking=thinking)
        selected = body["tool_choice"]
        assert (
            selected if isinstance(selected, str) else selected["function"]["name"]
        ) == choice
        assert body.get("parallel_tool_calls") is parallel
    finally:
        await provider.aclose()


async def test_google_ordinary_chat_keeps_auto_tool_choice(spec, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "test")
    provider = provider_class("google")(model="gemini-3-flash-preview")
    try:
        assert provider.build_request([], [spec.tool()])["tool_choice"] == "auto"
    finally:
        await provider.aclose()


@pytest.mark.parametrize(
    "model", ["gpt-5", "gpt-4o-2024-08-06", "gpt-4o-2024-05-13", "unknown-model"]
)
async def test_openai_uses_native_output_without_guessing_transport(
    model, spec, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    provider = provider_class("openai")(model=model)
    try:
        body = provider.build_request([], [], output_spec=spec)
        assert body["text"]["format"]["strict"] is True
        assert body["text"]["format"]["schema"] == spec.tool()["input_schema"]
        assert "tools" not in body
    finally:
        await provider.aclose()


@pytest.mark.parametrize(
    "vendor,model,field",
    [
        ("openai", "gpt-5", "max_output_tokens"),
        ("anthropic", "claude-sonnet-4-6", "max_tokens"),
        ("deepseek", "deepseek-flash", "max_tokens"),
    ],
)
async def test_explicit_total_output_budget(vendor, model, field, spec, monkeypatch):
    monkeypatch.setenv(f"{vendor.upper()}_API_KEY", "test")
    provider = provider_class(vendor)(model=model, max_output_tokens=12345)
    try:
        assert provider.build_request([], [], output_spec=spec)[field] == 12345
    finally:
        await provider.aclose()


@pytest.mark.parametrize(
    "vendor,model", [("openai", "gpt-5"), ("anthropic", "claude-sonnet-4-6")]
)
@pytest.mark.parametrize("structured", [False, True])
@pytest.mark.parametrize("observer_error", [False, True])
async def test_one_adapter_call_is_one_http_attempt(
    vendor, model, structured, observer_error, spec, monkeypatch, respx_mock
):
    monkeypatch.setenv(f"{vendor.upper()}_API_KEY", "test")
    provider = provider_class(vendor)(model=model, base_url="https://fixture.test")
    route = respx_mock.route(method="POST").respond(
        429,
        headers={"Retry-After": "7"},
        json={"error": {"type": "rate_limit_error", "message": "synthetic"}},
    )
    captured = []

    def observe(body):
        captured.append(deepcopy(body))
        # A diagnostic sink cannot edit the nested request actually sent.
        body.get("input", body.get("messages"))[-1]["content"] = "observer mutation"
        if observer_error:
            raise OSError("request log unavailable")

    try:
        with pytest.raises(ProviderTransientError) as caught:
            if structured:
                await provider.generate_structured(
                    [UserMessage("test")], spec, on_request=observe
                )
            else:
                await provider.chat([UserMessage("test")], [], on_request=observe)
        assert caught.value.status_code == 429
        assert caught.value.retry_after == 7
        assert route.call_count == len(captured) == 1
        assert json.loads(route.calls[0].request.content) == captured[0]
    finally:
        await provider.aclose()


@pytest.mark.parametrize("vendor", ["openai", "anthropic"])
@pytest.mark.parametrize("structured", [False, True])
async def test_request_observer_cancellation_stops_before_transport(
    vendor, structured, spec, monkeypatch, respx_mock
):
    monkeypatch.setenv(f"{vendor.upper()}_API_KEY", "test")
    provider = provider_class(vendor)(model="test", base_url="https://fixture.test")

    def cancel(body):
        raise asyncio.CancelledError("stop requested")

    try:
        with pytest.raises(asyncio.CancelledError, match="stop requested"):
            if structured:
                await provider.generate_structured([], spec, on_request=cancel)
            else:
                await provider.chat([], [], on_request=cancel)
        assert len(respx_mock.calls) == 0
    finally:
        await provider.aclose()


@pytest.mark.parametrize("vendor", ["openai", "anthropic"])
@pytest.mark.parametrize("kind", ["truncation", "refusal"])
async def test_terminal_signal_survives_malformed_tool_arguments(
    vendor, kind, spec, monkeypatch, respx_mock
):
    monkeypatch.setenv(f"{vendor.upper()}_API_KEY", "test")
    if vendor == "openai":
        body = {
            "status": "incomplete" if kind == "truncation" else "completed",
            "incomplete_details": {"reason": "max_output_tokens"}
            if kind == "truncation"
            else None,
            "output": [
                {"type": "function_call", "call_id": "incomplete"},
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "refusal", "refusal": "blocked"}]
                    if kind == "refusal"
                    else [],
                },
            ],
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }
    else:
        body = {
            "id": "r",
            "model": "test",
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "t", "name": "submit_turn", "input": []}
            ],
            "stop_reason": "max_tokens" if kind == "truncation" else "refusal",
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }
    provider = provider_class(vendor)(model="test", base_url="https://fixture.test")
    route = respx_mock.route(method="POST").respond(json=body)
    try:
        completion = await provider.generate_structured([], spec)
        assert completion.failure.kind == kind
        assert completion.reply.decode_error
        assert completion.reply.usage.completion_tokens == 20
        assert route.call_count == 1
    finally:
        await provider.aclose()


@pytest.mark.parametrize("budget", [0, -1, True, 1.5])
def test_invalid_total_budget_fails_before_client_creation(budget):
    with pytest.raises(ProviderConfigurationError, match="positive integer"):
        provider_class("openai")(model="gpt-5", max_output_tokens=budget)


async def test_anthropic_thinking_must_fit_explicit_total_budget(spec, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    provider = provider_class("anthropic")(
        model="claude-sonnet-4-5", max_output_tokens=1024
    )
    try:
        with pytest.raises(ProviderConfigurationError, match="thinking budget"):
            provider.build_request([], [], output_spec=spec, thinking="high")
    finally:
        await provider.aclose()


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None),
        ("3", 3),
        ("-2", 0),
        ("NaN", None),
        ("inf", None),
        ("garbage", None),
    ],
)
def test_retry_after_invalid_and_numeric(value, expected):
    assert retry_after_seconds(value) == expected


@freeze_time("2026-11-02T12:00:00Z")
def test_retry_after_http_date():
    future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=40))
    assert retry_after_seconds(future) == 40
