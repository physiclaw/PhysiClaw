"""Tests for `physiclaw.core.server.watch` — phone watchdog routes.

`fake_mcp` and `async_request` fixtures live in `conftest.py`.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from physiclaw.core.server import watch as watch_reg


def test_watch_register_wires_all_routes(fake_mcp) -> None:
    watch_reg.register(fake_mcp, physiclaw=MagicMock())

    by_path = {(p, ms[0]) for p, ms, _ in fake_mcp.routes}
    assert by_path == {
        ("/api/phone/watch", "GET"),
    }


# ---------- /api/phone/watch ----------


@pytest.mark.asyncio
async def test_watch_route_returns_watchdog_result(fake_mcp, async_request) -> None:
    pl = MagicMock()
    pl.perception.watch.return_value = {"wake": True, "reason": "x"}
    watch_reg.register(fake_mcp, pl)

    resp = await fake_mcp.get("/api/phone/watch")(async_request())

    assert json.loads(bytes(resp.body).decode()) == {"wake": True, "reason": "x"}


@pytest.mark.asyncio
async def test_watch_route_runtime_error_returns_no_wake(
    fake_mcp,
    async_request,
) -> None:
    pl = MagicMock()
    pl.perception.watch.side_effect = RuntimeError("not calibrated")
    watch_reg.register(fake_mcp, pl)

    resp = await fake_mcp.get("/api/phone/watch")(async_request())

    assert json.loads(bytes(resp.body).decode()) == {"wake": False, "reason": ""}


@pytest.mark.asyncio
async def test_watch_route_unexpected_exception_returns_503(
    fake_mcp,
    async_request,
) -> None:
    pl = MagicMock()
    pl.perception.watch.side_effect = Exception("kaboom")
    watch_reg.register(fake_mcp, pl)

    resp = await fake_mcp.get("/api/phone/watch")(async_request())

    assert resp.status_code == 503
