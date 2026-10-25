"""Tests for `physiclaw.core.server.hardware_setup` — hardware setup HTTP
handlers (moved from the old `core.hardware.handler`). The camera
identification algorithms live in `core.orchestration.camera_pick` and
are tested in `tests/core/orchestration/test_camera_pick.py`."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from physiclaw.core.server import hardware_setup
from physiclaw.core.server.hardware_setup import (
    handle_camera_preview,
    handle_connect_arm,
    handle_connect_camera,
    handle_disconnect_camera,
    handle_setup_page,
    handle_status,
)
from tests.core.conftest import stream_request, wire_locked


@pytest.mark.asyncio
async def test_handle_setup_page_serves_wizard(mocker) -> None:
    # Substitution lives in bridge.handler.render_phone_page_html.
    import physiclaw.core.bridge.handler as bridge_handler

    mocker.patch.object(
        bridge_handler,
        "bridge_base_urls",
        return_value=("http://device.local:8048", "http://10.0.0.5:8048"),
    )
    req = _fake_request()
    req.url = SimpleNamespace(port=8048)

    resp = await handle_setup_page(req)

    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    body = bytes(resp.body)
    assert b"Hardware Setup" in body
    # drives the same API the CLI does
    assert b"/api/calibrate/arm" in body
    # phone bridge URLs are substituted in so the QR renders inline (no iframe)
    assert b"__PHONE_URL__" not in body
    assert b"http://device.local:8048/bridge" in body
    assert b"<iframe" not in body


def _fake_request(
    json_obj: Any = None,
    path_params: dict | None = None,
    query_params: dict | None = None,
):
    data = b"" if json_obj is None else json.dumps(json_obj).encode()
    return stream_request(
        data, path_params=path_params or {}, query_params=query_params or {}
    )


def _read_json(response) -> dict:
    return json.loads(bytes(response.body).decode())


# ---------- handle_status ----------


@pytest.mark.asyncio
async def test_handle_status_returns_status_dict() -> None:
    rig = SimpleNamespace(status=lambda: {"arm": True, "cam": False})
    req = _fake_request()

    resp = await handle_status(req, rig)

    assert _read_json(resp) == {"arm": True, "cam": False}


# ---------- handle_connect_arm ----------


@pytest.mark.asyncio
async def test_handle_connect_arm_happy_path() -> None:
    rig = _rig_mock()

    resp = await handle_connect_arm(_fake_request(), rig)

    body = _read_json(resp)
    assert body["status"] == "ok"
    assert "Arm connected" in body["message"]
    rig.acquire.assert_called_once()
    rig.connect_arm.assert_called_once()
    rig.release.assert_called_once()


@pytest.mark.asyncio
async def test_handle_connect_arm_missing_device_is_409() -> None:
    """A missing GRBL board is the operator's to fix (plug it in) —
    client state, same 409 convention as the calibration preconditions."""
    from physiclaw.core.hardware.device import DeviceNotFound

    rig = _rig_mock()
    rig.connect_arm.side_effect = DeviceNotFound("GRBL device not found")

    resp = await handle_connect_arm(_fake_request(), rig)

    assert resp.status_code == 409
    assert "GRBL device not found" in _read_json(resp)["message"]


@pytest.mark.asyncio
async def test_handle_connect_camera_missing_device_is_409() -> None:
    from physiclaw.core.hardware.device import DeviceNotFound

    rig = _fake_rig_cam_index()
    rig.connect_camera.side_effect = DeviceNotFound("Cannot open camera index 3")
    phone = MagicMock()

    resp = await handle_connect_camera(_fake_request(json_obj={"index": 3}), rig, phone)

    assert resp.status_code == 409
    rig.release.assert_called_once()


@pytest.mark.asyncio
async def test_handle_connect_arm_releases_even_on_failure() -> None:
    rig = _rig_mock()
    rig.connect_arm.side_effect = RuntimeError("no port")

    resp = await handle_connect_arm(_fake_request(), rig)

    assert resp.status_code == 500
    body = _read_json(resp)
    assert body["status"] == "error"
    assert "no port" in body["message"]
    rig.release.assert_called_once()


# ---------- handle_connect_camera ----------


def _rig_mock() -> MagicMock:
    """A MagicMock rig whose ``locked()`` delegates to ``acquire()`` /
    ``release()`` (see ``wire_locked``), so the connect/disconnect tests assert
    the real acquire→release bracket rather than an inert context-manager
    mock."""
    return wire_locked(MagicMock(name="rig"))


def _fake_rig_cam_index(idx: int = 5) -> SimpleNamespace:
    """Build a fake rig with .cam.index and required methods."""
    rig = _rig_mock()
    rig.cam.index = idx
    rig.require_cam.return_value = rig.cam
    return rig


@pytest.mark.asyncio
async def test_handle_connect_camera_explicit_index(mocker) -> None:
    rig = _fake_rig_cam_index(idx=2)
    phone = MagicMock()
    auto_spy = mocker.patch.object(hardware_setup, "resolve_auto_index")

    resp = await handle_connect_camera(
        _fake_request(json_obj={"index": 2}),
        rig,
        phone,
    )

    body = _read_json(resp)
    assert body["status"] == "ok"
    assert body["index"] == 2
    rig.connect_camera.assert_called_once_with(2)
    # No auto-pick on an explicit index.
    auto_spy.assert_not_called()


@pytest.mark.asyncio
async def test_handle_connect_camera_auto_pick_happy_path(mocker) -> None:
    rig = _fake_rig_cam_index(idx=4)
    phone = MagicMock()
    auto_spy = mocker.patch.object(hardware_setup, "resolve_auto_index", return_value=4)

    resp = await handle_connect_camera(
        _fake_request(json_obj={"index": "auto"}),
        rig,
        phone,
    )

    body = _read_json(resp)
    assert body["status"] == "ok"
    assert body["index"] == 4
    auto_spy.assert_called_once_with(rig, phone)
    rig.connect_camera.assert_called_once_with(4)


@pytest.mark.asyncio
async def test_handle_connect_camera_auto_pick_treats_missing_body_as_auto(
    mocker,
) -> None:
    rig = _fake_rig_cam_index(idx=1)
    phone = MagicMock()
    mocker.patch.object(hardware_setup, "resolve_auto_index", return_value=1)

    resp = await handle_connect_camera(
        _fake_request(),
        rig,
        phone,
    )

    assert _read_json(resp)["status"] == "ok"


@pytest.mark.asyncio
async def test_handle_connect_camera_auto_pick_failure_returns_500(mocker) -> None:
    rig = _fake_rig_cam_index()
    phone = MagicMock()
    mocker.patch.object(
        hardware_setup,
        "resolve_auto_index",
        side_effect=RuntimeError("auto-pick: /bridge page not polling"),
    )

    resp = await handle_connect_camera(
        _fake_request(json_obj={"index": "auto"}),
        rig,
        phone,
    )

    assert resp.status_code == 500
    body = _read_json(resp)
    assert "auto-pick" in body["message"]
    # Never reached the connect step.
    rig.connect_camera.assert_not_called()


@pytest.mark.asyncio
async def test_handle_connect_camera_releases_on_connect_failure(mocker) -> None:
    rig = _fake_rig_cam_index()
    rig.connect_camera.side_effect = RuntimeError("usb error")
    phone = MagicMock()

    resp = await handle_connect_camera(
        _fake_request(json_obj={"index": 0}),
        rig,
        phone,
    )

    assert resp.status_code == 500
    rig.release.assert_called_once()


@pytest.mark.asyncio
async def test_handle_connect_camera_stores_index_in_calibration(mocker) -> None:
    rig = _fake_rig_cam_index(idx=2)
    phone = MagicMock()

    await handle_connect_camera(
        _fake_request(json_obj={"index": 2}),
        rig,
        phone,
    )

    # Stored on the calibration namespace as int.
    assert rig.calibration.cam_index == 2


# ---------- handle_disconnect_camera ----------


@pytest.mark.asyncio
async def test_handle_disconnect_camera_releases_when_connected() -> None:
    rig = _rig_mock()
    rig.disconnect_camera.return_value = True

    resp = await handle_disconnect_camera(_fake_request(), rig)

    body = _read_json(resp)
    assert body == {"status": "ok", "released": True}
    rig.disconnect_camera.assert_called_once()
    rig.release.assert_called_once()


@pytest.mark.asyncio
async def test_handle_disconnect_camera_idempotent_when_no_camera() -> None:
    rig = _rig_mock()
    rig.disconnect_camera.return_value = False

    resp = await handle_disconnect_camera(_fake_request(), rig)

    body = _read_json(resp)
    assert body == {"status": "ok", "released": False}


@pytest.mark.asyncio
async def test_handle_disconnect_camera_releases_lock_on_failure() -> None:
    rig = _rig_mock()
    rig.disconnect_camera.side_effect = RuntimeError("close failed")

    resp = await handle_disconnect_camera(_fake_request(), rig)

    assert resp.status_code == 500
    rig.release.assert_called_once()


# ---------- handle_camera_preview ----------


@pytest.mark.asyncio
async def test_handle_camera_preview_happy_path(mocker) -> None:
    rig = _rig_mock()
    mocker.patch.object(hardware_setup, "camera_preview", return_value=b"JPEG-bytes")

    resp = await handle_camera_preview(
        _fake_request(path_params={"index": "3"}, query_params={}),
        rig,
    )

    body = _read_json(resp)
    assert body["status"] == "ok"
    assert body["index"] == 3
    assert body["image"] == base64.b64encode(b"JPEG-bytes").decode()


@pytest.mark.asyncio
async def test_handle_camera_preview_passes_watermark_query_param(mocker) -> None:
    rig = _rig_mock()
    spy = mocker.patch.object(hardware_setup, "camera_preview", return_value=b"x")

    await handle_camera_preview(
        _fake_request(path_params={"index": "5"}, query_params={"watermark": "1"}),
        rig,
    )

    spy.assert_called_once_with(5, True, connected=rig.cam)


@pytest.mark.asyncio
async def test_handle_camera_preview_default_watermark_is_false(mocker) -> None:
    rig = _rig_mock()
    spy = mocker.patch.object(hardware_setup, "camera_preview", return_value=b"x")

    await handle_camera_preview(
        _fake_request(path_params={"index": "0"}),
        rig,
    )

    spy.assert_called_once_with(0, False, connected=rig.cam)


@pytest.mark.asyncio
async def test_handle_camera_preview_returns_404_on_capture_failure(mocker) -> None:
    rig = _rig_mock()
    mocker.patch.object(
        hardware_setup,
        "camera_preview",
        side_effect=RuntimeError("no frame"),
    )

    resp = await handle_camera_preview(
        _fake_request(path_params={"index": "0"}),
        rig,
    )

    assert resp.status_code == 404
    body = _read_json(resp)
    assert body["status"] == "error"
    assert "no frame" in body["message"]


async def test_regression_auto_camera_busy_does_not_probe_or_change_page(mocker):
    rig = _rig_mock()
    rig.acquire.side_effect = RuntimeError("busy")
    phone = MagicMock()
    pick = mocker.patch.object(hardware_setup, "resolve_auto_index", return_value=2)

    response = await handle_connect_camera(_fake_request({"index": "auto"}), rig, phone)

    assert response.status_code == 500
    pick.assert_not_called()
    rig.connect_camera.assert_not_called()


async def test_regression_camera_response_keeps_index_from_its_own_connection():
    rig = _fake_rig_cam_index(idx=2)
    rig.release.side_effect = lambda: setattr(rig, "cam", None)

    response = await handle_connect_camera(
        _fake_request({"index": 2}), rig, MagicMock()
    )

    assert response.status_code == 200
    assert _read_json(response)["index"] == 2


@pytest.mark.parametrize("index", [True, False, 1.5, "2", -1, [], {}, float("inf")])
async def test_regression_camera_invalid_index_cannot_select_another_device(index):
    rig = _fake_rig_cam_index()

    response = await handle_connect_camera(
        _fake_request({"index": index}), rig, MagicMock()
    )

    assert response.status_code == 400
    rig.acquire.assert_not_called()
    rig.connect_camera.assert_not_called()


@pytest.mark.parametrize("raw", [b"{", b"null", b"[]", b"true", b'"auto"'])
async def test_regression_camera_malformed_body_does_not_start_auto_pick(mocker, raw):
    from starlette.requests import Request

    async def receive():
        return {"type": "http.request", "body": raw}

    req = Request({"type": "http", "method": "POST", "headers": []}, receive)
    rig = _rig_mock()
    pick = mocker.patch.object(hardware_setup, "resolve_auto_index", return_value=1)

    response = await handle_connect_camera(req, rig, MagicMock())

    assert response.status_code == 400
    pick.assert_not_called()
    rig.acquire.assert_not_called()


async def test_camera_preview_busy_does_not_open_or_capture(mocker):
    rig = _rig_mock()
    rig.acquire.side_effect = RuntimeError("busy")
    preview = mocker.patch.object(hardware_setup, "camera_preview")

    response = await handle_camera_preview(
        _fake_request(path_params={"index": "0"}), rig
    )

    assert response.status_code == 404
    preview.assert_not_called()


async def test_camera_preview_route_reuses_exclusively_owned_camera(fake_mcp, mocker):
    import cv2
    import numpy as np

    from physiclaw.core.orchestration import camera_pick
    from physiclaw.core.orchestration.rig import HardwareRig
    from physiclaw.core.server.hardware import register

    rig = HardwareRig()
    rig._cam = MagicMock(index=2)
    rig.cam.raw_frame.return_value = np.full((12, 20, 3), 80, np.uint8)
    opener = mocker.patch.object(
        camera_pick, "Camera", side_effect=RuntimeError("owned")
    )
    register(fake_mcp, rig, MagicMock())

    response = await fake_mcp.get("/api/camera-preview/{index}")(
        _fake_request(path_params={"index": "2"})
    )

    assert response.status_code == 200
    jpeg = base64.b64decode(_read_json(response)["image"])
    assert cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR).shape == (
        12,
        20,
        3,
    )
    opener.assert_not_called()
    rig.cam.close.assert_not_called()
