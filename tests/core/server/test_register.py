"""Tests for the route-registration modules under `physiclaw.core.server.*`.

`bridge.py`, `calibration.py`, `hardware.py`, and `watch.py` are pure
wiring — they take an `mcp` object and register Starlette routes via
`@mcp.custom_route(...)`. We feed each `register()` a mock that records
registrations, then invoke each handler and verify it dispatches to the
underlying request handler with the right state objects.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from physiclaw.core.server import bridge as bridge_reg
from physiclaw.core.server import calibration as calib_reg
from physiclaw.core.server import hardware as hw_reg
from physiclaw.core.server import watch as watch_reg


pytestmark = [pytest.mark.integration]


# ---------- helpers ----------


class FakeMcp:
    """Records every (path, methods, handler) registered."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, tuple[str, ...], Any]] = []

    def custom_route(self, path: str, methods: list[str]):
        def deco(fn):
            self.routes.append((path, tuple(methods), fn))
            return fn
        return deco

    def get(self, path: str, method: str = "GET"):
        for p, ms, fn in self.routes:
            if p == path and method in ms:
                return fn
        raise KeyError(f"no route {method} {path}")


def _async_request(json_obj: dict | None = None) -> SimpleNamespace:
    async def _json():
        return json_obj or {}

    return SimpleNamespace(
        json=_json,
        path_params={},
        query_params={},
    )


# ---------- bridge.register ----------


def test_bridge_register_wires_all_routes() -> None:
    mcp = FakeMcp()
    bridge_reg.register(
        mcp, physiclaw=MagicMock(), bridge=MagicMock(),
        calib=MagicMock(), phone=MagicMock(),
    )

    # Every documented path is registered with the correct method.
    expected = {
        ("/bridge", "GET"),
        ("/api/bridge/state", "GET"),
        ("/api/bridge/qr", "GET"),
        ("/api/bridge/tapped", "POST"),
        ("/api/bridge/screen-dimension", "POST"),
        ("/api/bridge/screenshot", "POST"),
        ("/api/bridge/clipboard", "GET"),
        ("/api/bridge/switch", "POST"),
        ("/api/bridge/touch", "POST"),
    }
    actual = {(p, ms[0]) for p, ms, _ in mcp.routes}
    assert expected == actual


@pytest.mark.asyncio
async def test_bridge_routes_forward_to_handlers(mocker) -> None:
    """Each route, when called, must delegate to the matching handler
    with the right positional args."""
    spies = {
        "serve_bridge_page": mocker.patch.object(bridge_reg, "serve_bridge_page"),
        "serve_qr_page": mocker.patch.object(bridge_reg, "serve_qr_page"),
        "handle_phone_state": mocker.patch.object(bridge_reg, "handle_phone_state"),
        "handle_clipboard_copied": mocker.patch.object(
            bridge_reg, "handle_clipboard_copied",
        ),
        "handle_clipboard_fetch": mocker.patch.object(
            bridge_reg, "handle_clipboard_fetch",
        ),
        "handle_mode_switch": mocker.patch.object(bridge_reg, "handle_mode_switch"),
        "handle_screen_dimension": mocker.patch.object(
            bridge_reg, "handle_screen_dimension",
        ),
        "handle_screenshot_upload": mocker.patch.object(
            bridge_reg, "handle_screenshot_upload",
        ),
        "handle_calib_touch": mocker.patch.object(bridge_reg, "handle_calib_touch"),
    }
    # Each spy returns an awaitable that resolves to a sentinel.
    async def _ok(*a, **kw):
        return "ok"
    for s in spies.values():
        s.side_effect = _ok

    mcp = FakeMcp()
    pl, br, cb, ph = MagicMock(), MagicMock(), MagicMock(), MagicMock()
    bridge_reg.register(mcp, pl, br, cb, ph)

    req = _async_request()

    await mcp.get("/bridge")(req)
    spies["serve_bridge_page"].assert_called_once_with(req)

    await mcp.get("/api/bridge/state")(req)
    spies["handle_phone_state"].assert_called_once_with(req, ph)

    await mcp.get("/api/bridge/qr")(req)
    spies["serve_qr_page"].assert_called_once_with(req)

    await mcp.get("/api/bridge/tapped", "POST")(req)
    spies["handle_clipboard_copied"].assert_called_once_with(req, br)

    await mcp.get("/api/bridge/screen-dimension", "POST")(req)
    spies["handle_screen_dimension"].assert_called_once_with(req, cb)

    await mcp.get("/api/bridge/screenshot", "POST")(req)
    spies["handle_screenshot_upload"].assert_called_once_with(req, br)

    await mcp.get("/api/bridge/clipboard")(req)
    spies["handle_clipboard_fetch"].assert_called_once_with(req, br)

    await mcp.get("/api/bridge/switch", "POST")(req)
    spies["handle_mode_switch"].assert_called_once_with(req, ph)

    await mcp.get("/api/bridge/touch", "POST")(req)
    spies["handle_calib_touch"].assert_called_once_with(req, cb)


# ---------- calibration.register ----------


def test_calibration_register_wires_all_routes() -> None:
    mcp = FakeMcp()
    calib_reg.register(
        mcp, physiclaw=MagicMock(), bridge=MagicMock(),
        calib=MagicMock(), phone=MagicMock(),
    )

    expected = {
        "/api/calibrate/viewport-shift",
        "/api/calibrate/arm",
        "/api/calibrate/camera",
        "/api/calibrate/camera-mapping",
        "/api/calibrate/validate",
        "/api/calibrate/trace-edge",
        "/api/calibrate/assistive-touch/show",
        "/api/calibrate/assistive-touch/verify",
    }
    paths = {p for p, _, _ in mcp.routes}
    assert expected == paths
    assert all(ms == ("POST",) for _, ms, _ in mcp.routes)


@pytest.mark.asyncio
async def test_calibration_routes_forward_to_handlers(mocker) -> None:
    handlers = [
        "handle_measure_viewport_shift",
        "handle_calibrate_arm",
        "handle_calibrate_camera_frame",
        "handle_compute_camera_mapping",
        "handle_validate_calibration",
        "handle_trace_edge",
        "handle_show_assistive_touch",
        "handle_verify_assistive_touch",
    ]
    spies = {h: mocker.patch.object(calib_reg, h) for h in handlers}

    async def _ok(*a, **kw):
        return "ok"
    for s in spies.values():
        s.side_effect = _ok

    mcp = FakeMcp()
    pl, br, cb, ph = MagicMock(), MagicMock(), MagicMock(), MagicMock()
    calib_reg.register(mcp, pl, br, cb, ph)

    req = _async_request()
    routes_to_handlers = {
        "/api/calibrate/viewport-shift": "handle_measure_viewport_shift",
        "/api/calibrate/arm": "handle_calibrate_arm",
        "/api/calibrate/camera": "handle_calibrate_camera_frame",
        "/api/calibrate/camera-mapping": "handle_compute_camera_mapping",
        "/api/calibrate/validate": "handle_validate_calibration",
        "/api/calibrate/trace-edge": "handle_trace_edge",
        "/api/calibrate/assistive-touch/show": "handle_show_assistive_touch",
        "/api/calibrate/assistive-touch/verify": "handle_verify_assistive_touch",
    }
    for path, hname in routes_to_handlers.items():
        await mcp.get(path, "POST")(req)
        assert spies[hname].called, f"{hname} not invoked from {path}"


# ---------- hardware.register ----------


def test_hardware_register_wires_all_routes() -> None:
    mcp = FakeMcp()
    hw_reg.register(mcp, physiclaw=MagicMock(), phone=MagicMock())

    by_path = {(p, ms[0]) for p, ms, _ in mcp.routes}
    assert by_path == {
        ("/api/status", "GET"),
        ("/api/connect-arm", "POST"),
        ("/api/connect-camera", "POST"),
        ("/api/camera-preview/{index}", "GET"),
    }


@pytest.mark.asyncio
async def test_hardware_routes_forward_to_handlers(mocker) -> None:
    spies = {
        "handle_status": mocker.patch.object(hw_reg, "handle_status"),
        "handle_connect_arm": mocker.patch.object(hw_reg, "handle_connect_arm"),
        "handle_connect_camera": mocker.patch.object(hw_reg, "handle_connect_camera"),
        "handle_camera_preview": mocker.patch.object(hw_reg, "handle_camera_preview"),
    }

    async def _ok(*a, **kw):
        return "ok"
    for s in spies.values():
        s.side_effect = _ok

    mcp = FakeMcp()
    pl, ph = MagicMock(), MagicMock()
    hw_reg.register(mcp, pl, ph)

    req = _async_request()
    await mcp.get("/api/status")(req)
    spies["handle_status"].assert_called_once_with(req, pl)

    await mcp.get("/api/connect-arm", "POST")(req)
    spies["handle_connect_arm"].assert_called_once_with(req, pl)

    await mcp.get("/api/connect-camera", "POST")(req)
    spies["handle_connect_camera"].assert_called_once_with(req, pl, ph)

    await mcp.get("/api/camera-preview/{index}")(req)
    spies["handle_camera_preview"].assert_called_once_with(req)


# ---------- watch.register ----------


def test_watch_register_wires_all_routes() -> None:
    mcp = FakeMcp()
    watch_reg.register(mcp, physiclaw=MagicMock())

    by_path = {(p, ms[0]) for p, ms, _ in mcp.routes}
    assert by_path == {
        ("/api/phone/watch", "GET"),
        ("/api/phone/home", "POST"),
        ("/api/ready", "POST"),
    }


@pytest.mark.asyncio
async def test_watch_route_returns_watchdog_result() -> None:
    mcp = FakeMcp()
    pl = MagicMock()
    pl.watch.return_value = {"wake": True, "reason": "x"}
    watch_reg.register(mcp, pl)

    resp = await mcp.get("/api/phone/watch")(_async_request())

    import json
    assert json.loads(bytes(resp.body).decode()) == {"wake": True, "reason": "x"}


@pytest.mark.asyncio
async def test_watch_route_runtime_error_returns_no_wake() -> None:
    mcp = FakeMcp()
    pl = MagicMock()
    pl.watch.side_effect = RuntimeError("not calibrated")
    watch_reg.register(mcp, pl)

    resp = await mcp.get("/api/phone/watch")(_async_request())

    import json
    body = json.loads(bytes(resp.body).decode())
    assert body == {"wake": False, "reason": ""}


@pytest.mark.asyncio
async def test_watch_route_unexpected_exception_returns_503() -> None:
    mcp = FakeMcp()
    pl = MagicMock()
    pl.watch.side_effect = Exception("kaboom")
    watch_reg.register(mcp, pl)

    resp = await mcp.get("/api/phone/watch")(_async_request())

    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_home_screen_route_dispatches() -> None:
    mcp = FakeMcp()
    pl = MagicMock()
    watch_reg.register(mcp, pl)

    resp = await mcp.get("/api/phone/home", "POST")(_async_request())

    pl.home_screen.assert_called_once()
    import json
    assert json.loads(bytes(resp.body).decode()) == {"ok": True}


@pytest.mark.asyncio
async def test_home_screen_route_returns_503_on_failure() -> None:
    mcp = FakeMcp()
    pl = MagicMock()
    pl.home_screen.side_effect = RuntimeError("arm jammed")
    watch_reg.register(mcp, pl)

    resp = await mcp.get("/api/phone/home", "POST")(_async_request())

    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_ready_route_marks_and_returns_ready_state() -> None:
    mcp = FakeMcp()
    pl = MagicMock()
    pl.ready = True
    watch_reg.register(mcp, pl)

    resp = await mcp.get("/api/ready", "POST")(_async_request())

    pl.mark_ready.assert_called_once()
    import json
    assert json.loads(bytes(resp.body).decode()) == {"ok": True, "ready": True}
