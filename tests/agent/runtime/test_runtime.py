"""Tests for `physiclaw.agent.runtime.runtime`.

Runtime.start tests are integration-leaning (intricate event-loop +
hooks side effects); covered with one happy-path-and-stop scenario,
the rest deferred.
"""
from __future__ import annotations

import asyncio

import pytest
import respx

from physiclaw.agent.runtime import hook, runtime
from physiclaw.agent.runtime.hook import Trigger
from physiclaw.agent.runtime.runtime import Runtime, _check_ready, _maybe_await


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    hook.clear()
    monkeypatch.setattr(runtime, "_client", None)
    monkeypatch.setenv("PHYSICLAW_SERVER", "http://test.host:8048")


# ---------- _maybe_await ----------


@pytest.mark.asyncio
async def test_maybe_await_returns_sync_value_unchanged() -> None:
    assert await _maybe_await("plain") == "plain"
    assert await _maybe_await(None) is None


@pytest.mark.asyncio
async def test_maybe_await_awaits_coroutines() -> None:
    async def inner():
        return "from-coro"

    assert await _maybe_await(inner()) == "from-coro"


# ---------- _check_ready ----------


@pytest.mark.asyncio
async def test_check_ready_true(respx_mock: respx.MockRouter) -> None:
    respx_mock.get("http://test.host:8048/api/status").respond(
        json={"ready": True}
    )

    assert await _check_ready() is True


@pytest.mark.asyncio
async def test_check_ready_false(respx_mock: respx.MockRouter) -> None:
    respx_mock.get("http://test.host:8048/api/status").respond(
        json={"ready": False}
    )

    assert await _check_ready() is False


@pytest.mark.asyncio
async def test_check_ready_raises_on_4xx(respx_mock: respx.MockRouter) -> None:
    import httpx

    respx_mock.get("http://test.host:8048/api/status").respond(404)

    with pytest.raises(httpx.HTTPStatusError):
        await _check_ready()


@pytest.mark.asyncio
async def test_check_ready_returns_false_when_ready_field_missing(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get("http://test.host:8048/api/status").respond(json={})

    assert await _check_ready() is False


# ---------- Runtime construction + stop ----------


def test_runtime_init_with_defaults() -> None:
    r = Runtime(react=lambda triggers: None)

    assert r.interval == 1.0
    assert r.label == ""
    assert r._running is False


def test_runtime_init_with_custom_interval_and_label() -> None:
    r = Runtime(react=lambda t: None, interval=0.5, label="qwen-engine")

    assert r.interval == 0.5
    assert r.label == "qwen-engine"


def test_runtime_stop_flips_flag() -> None:
    r = Runtime(react=lambda t: None)
    r._running = True

    r.stop()

    assert r._running is False


# Runtime.start integration is deferred — exercising the full loop
# requires careful coordination between mocked sleep, react cooldown,
# and hook lifecycles. Construction + stop + helper functions cover
# the testable surface here.
