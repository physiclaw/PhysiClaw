"""Setup completion routes retain the asynchronous readiness contract."""

import json
from unittest.mock import MagicMock

import pytest

from physiclaw.core.setup import completion_routes


def test_completion_register_wires_all_routes(fake_mcp):
    completion_routes.register(fake_mcp, MagicMock())

    paths = {(p, ms[0]) for p, ms, _ in fake_mcp.routes}

    assert paths == {("/api/phone/home", "POST"), ("/api/ready", "POST")}


# ---------- /api/phone/home ----------


@pytest.mark.asyncio
async def test_home_screen_route_dispatches(fake_mcp, async_request) -> None:
    pl = MagicMock()
    completion_routes.register(fake_mcp, pl)

    resp = await fake_mcp.get("/api/phone/home", "POST")(async_request())

    pl.home_screen.assert_called_once()
    assert json.loads(bytes(resp.body).decode()) == {"ok": True}


@pytest.mark.asyncio
async def test_home_screen_route_returns_503_on_failure(
    fake_mcp,
    async_request,
) -> None:
    pl = MagicMock()
    pl.home_screen.side_effect = RuntimeError("arm jammed")
    completion_routes.register(fake_mcp, pl)

    resp = await fake_mcp.get("/api/phone/home", "POST")(async_request())

    assert resp.status_code == 503


# ---------- /api/ready ----------


@pytest.mark.asyncio
async def test_ready_route_returns_ok_immediately(
    fake_mcp,
    async_request,
) -> None:
    pl = MagicMock()
    completion_routes.register(fake_mcp, pl)

    resp = await fake_mcp.get("/api/ready", "POST")(async_request())

    assert json.loads(bytes(resp.body).decode()) == {"ok": True}


@pytest.mark.asyncio
async def test_ready_route_fires_become_ready_off_thread(
    fake_mcp, async_request
) -> None:
    # Fire-and-forget on the default executor: the response must not wait
    # for the settle, but become_ready (settle THEN mark) must run.
    import asyncio
    import threading

    from tests.timing_helpers import WAIT_TIMEOUT, wait_for

    pl = MagicMock()
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    resume = threading.Event()
    finished = asyncio.Event()
    main_thread = threading.get_ident()
    errors = []

    def become_ready():
        try:
            assert threading.get_ident() != main_thread
            loop.call_soon_threadsafe(started.set)
            assert resume.wait(WAIT_TIMEOUT)
        except BaseException as exc:
            errors.append(exc)
        finally:
            loop.call_soon_threadsafe(finished.set)

    pl.become_ready.side_effect = become_ready
    completion_routes.register(fake_mcp, pl)

    try:
        await wait_for(fake_mcp.get("/api/ready", "POST")(async_request()))
        await wait_for(started.wait())
        assert not finished.is_set()
    finally:
        resume.set()
        await wait_for(finished.wait())
    assert errors == []
    pl.become_ready.assert_called_once()
