"""Tests for `physiclaw.agent.hooks.poll` — phone watchdog hook.

HTTP responses are mocked so tests don't hit the real server.

Accepted mutation survivors: two log/diagnostic decorations, two equivalent
False-to-None resets of the truth-tested blip flag, and a 10-to-11-second
request timeout variation. There is no exact timing SLA; both keep requests
bounded. Startup state, registration, malformed envelopes and recovery are
covered through the public hook and HTTP boundary.
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest
import respx
from hypothesis import given
from hypothesis import strategies as st

from physiclaw.agent.hooks import poll
from physiclaw.agent.hooks.poll import phone_watch
from physiclaw.agent.runtime.hook import Trigger, check_hooks


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Module-level _client + _in_blip state must reset per test."""
    monkeypatch.setattr(poll, "_client", None)
    monkeypatch.setattr(poll, "_in_blip", False)


@pytest.fixture(autouse=True)
def _stub_server_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHYSICLAW_SERVER", "http://test.host:8048")


@pytest.mark.asyncio
async def test_phone_watch_returns_none_when_no_wake(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get("http://test.host:8048/api/phone/watch").respond(
        json={"wake": False}
    )

    assert await phone_watch() is None


@pytest.mark.asyncio
async def test_phone_watch_returns_trigger_when_wake_true(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get("http://test.host:8048/api/phone/watch").respond(
        json={"wake": True, "reason": "screen changed"},
    )

    out = await phone_watch()

    assert isinstance(out, Trigger)
    assert out.description == "screen changed"
    assert out.source == "phone"


@pytest.mark.asyncio
async def test_phone_watch_uses_default_reason_when_missing(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get("http://test.host:8048/api/phone/watch").respond(json={"wake": True})

    out = await phone_watch()

    assert out.description == "phone screen changed"


@pytest.mark.asyncio
async def test_phone_watch_returns_none_on_transport_error(
    respx_mock: respx.MockRouter,
) -> None:
    import httpx

    respx_mock.get("http://test.host:8048/api/phone/watch").mock(
        side_effect=httpx.ConnectError("boom")
    )

    assert await phone_watch() is None


@pytest.mark.asyncio
async def test_phone_watch_logs_warning_only_on_first_failure(
    respx_mock: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    import httpx

    respx_mock.get("http://test.host:8048/api/phone/watch").mock(
        side_effect=httpx.ConnectError("boom")
    )

    with caplog.at_level(logging.WARNING, logger="physiclaw.agent.hooks.poll"):
        await phone_watch()
        await phone_watch()
        await phone_watch()

    # Only the first failure logs; subsequent suppressed by _in_blip.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


@pytest.mark.asyncio
async def test_phone_watch_resets_blip_state_on_recovery(
    respx_mock: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    import httpx

    respx_mock.get("http://test.host:8048/api/phone/watch").mock(
        side_effect=[
            httpx.ConnectError("first"),
            httpx.Response(200, json={"wake": False}),
            httpx.ConnectError("second"),
        ]
    )

    with caplog.at_level(logging.WARNING, logger="physiclaw.agent.hooks.poll"):
        await phone_watch()
        await phone_watch()
        await phone_watch()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    # Two warnings — one per dropout (recovery resets _in_blip).
    assert len(warnings) == 2


@pytest.mark.asyncio
async def test_phone_watch_returns_none_on_4xx(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get("http://test.host:8048/api/phone/watch").respond(404)

    assert await phone_watch() is None


@pytest.mark.asyncio
async def test_phone_watch_non_json_200_is_a_blip_not_a_crash(
    respx_mock: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    """A 200 with a non-JSON body (server mid-restart, proxy interception)
    must follow the warn-once blip path, not raise out of the hook."""
    import logging

    respx_mock.get("http://test.host:8048/api/phone/watch").respond(
        content=b"<html>Service restarting</html>", content_type="text/html"
    )

    with caplog.at_level(logging.WARNING, logger="physiclaw.agent.hooks.poll"):
        assert await phone_watch() is None
        assert await phone_watch() is None

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1  # warn-once, like any other blip


@pytest.mark.parametrize(
    "payload",
    [None, [], "ok", 1, {}, {"wake": "false"}, {"wake": 1}, {"wake": []}],
)
@pytest.mark.asyncio
async def test_regression_malformed_watch_payload_is_a_warn_once_blip(
    respx_mock, caplog, payload
):
    respx_mock.get("http://test.host:8048/api/phone/watch").respond(
        content=json.dumps(payload), content_type="application/json"
    )

    first = await phone_watch()
    second = await phone_watch()

    assert first is None and second is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "phone watch poll failed" in warnings[0].message


@pytest.mark.parametrize("reason", [None, "", " \n ", 123, [], {"text": "changed"}])
@pytest.mark.asyncio
async def test_verified_wake_with_unusable_reason_keeps_a_readable_default(
    respx_mock, reason
):
    respx_mock.get("http://test.host:8048/api/phone/watch").respond(
        json={"wake": True, "reason": reason}
    )

    trigger = await phone_watch()

    assert trigger == Trigger(description="phone screen changed", source="phone")


@pytest.mark.asyncio
async def test_malformed_payload_does_not_reset_an_existing_blip(respx_mock, caplog):
    respx_mock.get("http://test.host:8048/api/phone/watch").mock(
        side_effect=[
            httpx.ConnectError("offline"),
            httpx.Response(200, json=[]),
            httpx.Response(200, json={"wake": False}),
            httpx.Response(200, json={"wake": "true"}),
        ]
    )

    results = [await phone_watch() for _ in range(4)]

    assert results == [None] * 4
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2


@pytest.mark.asyncio
async def test_cancellation_propagates_without_becoming_a_watch_failure(
    respx_mock, caplog
):
    respx_mock.get("http://test.host:8048/api/phone/watch").mock(
        side_effect=_cancel_watch_request
    )

    with pytest.raises(asyncio.CancelledError):
        await phone_watch()

    assert not caplog.records


def _cancel_watch_request(request):
    raise asyncio.CancelledError


async def _poll_document(document):
    from unittest.mock import patch

    with respx.mock as router:
        router.get("http://watch.test/api/phone/watch").respond(
            content=json.dumps(document), content_type="application/json"
        )
        # Avoid loading real TLS certificates for every Hypothesis example;
        # that unrelated I/O can exceed its deadline on a busy machine.
        async with httpx.AsyncClient(
            base_url="http://watch.test",
            trust_env=False,
            transport=httpx.MockTransport(router.async_handler),
        ) as client:
            with (
                patch.object(poll, "_client", client),
                patch.object(poll, "_in_blip", False),
            ):
                return await phone_watch()


@pytest.mark.asyncio
@given(value=st.one_of(st.none(), st.integers(), st.text(), st.lists(st.integers())))
async def test_non_boolean_watch_flags_never_create_a_wake(value):
    trigger = await _poll_document({"wake": value, "reason": "screen changed"})

    assert trigger is None


@pytest.mark.asyncio
@given(reason=st.text().filter(lambda value: bool(value.strip())))
async def test_valid_watch_reasons_preserve_the_exact_unicode_text(reason):
    trigger = await _poll_document({"wake": True, "reason": reason})

    assert trigger == Trigger(description=reason, source="phone")


@pytest.mark.asyncio
async def test_phone_watch_registers_with_a_usable_initial_client(
    fresh_hook_module, respx_mock
):
    fresh_hook_module(poll, "phone_watch")
    respx_mock.get("http://test.host:8048/api/phone/watch").respond(
        json={"wake": True, "reason": "fresh process"}
    )

    triggers = await check_hooks()

    assert triggers == [Trigger(description="fresh process", source="phone")]


@pytest.mark.asyncio
async def test_first_failure_after_import_is_not_silently_suppressed(
    fresh_hook_module, respx_mock, caplog
):
    fresh_hook_module(poll, "phone_watch")
    respx_mock.get("http://test.host:8048/api/phone/watch").respond(json=[])

    triggers = await check_hooks()

    assert triggers == []
    assert "phone watch poll failed" in caplog.text
