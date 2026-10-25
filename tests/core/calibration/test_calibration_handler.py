"""Tests for `physiclaw.core.calibration.handler` — calibration HTTP routes."""

from __future__ import annotations

import dataclasses
import json
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest

from physiclaw.core.bridge import BridgeState, CalibrationState, PageState
from physiclaw.core.calibration import handler
from physiclaw.core.calibration import validate as validate_mod
from physiclaw.core.calibration.handler import (
    _err,
    _ok,
    _read_flag,
    _run_blocking,
    handle_calibrate_arm,
    handle_calibrate_camera_frame,
    handle_compute_camera_mapping,
    handle_measure_viewport_shift,
    handle_show_assistive_touch,
    handle_trace_edge,
    handle_validate_calibration,
    handle_verify_assistive_touch,
)
from physiclaw.core.calibration.transforms import ViewportShift
from physiclaw.core.orchestration import rig as rig_mod
from tests.core.conftest import stream_request, wire_locked


def _rig_mock() -> MagicMock:
    """A MagicMock rig whose ``locked()`` delegates to ``acquire()`` /
    ``release()`` (see ``wire_locked``), so the handler tests assert the real
    lock bracket (acquire → park-on-failure → release) rather than an inert
    context-manager mock."""
    return wire_locked(MagicMock(name="rig"))


def _fake_request(json_obj: Any = None, *, body_bytes: bytes | None = None):
    if body_bytes is not None:
        return stream_request(body_bytes)
    data = b"" if json_obj is None else json.dumps(json_obj).encode()
    return stream_request(data)


def _read_json(response) -> dict:
    return json.loads(bytes(response.body).decode())


# ---------- helpers ----------


def test_ok_wraps_payload() -> None:
    resp = _ok({"a": 1})

    assert _read_json(resp) == {"status": "ok", "a": 1}


def test_err_default_500() -> None:
    resp = _err("boom")

    assert resp.status_code == 500
    assert _read_json(resp) == {"status": "error", "message": "boom"}


def test_err_custom_status_code() -> None:
    resp = _err("bad input", status_code=400)

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_read_flag_parses_json() -> None:
    assert await _read_flag(_fake_request(json_obj={"fresh": True}), "fresh") is True


@pytest.mark.asyncio
async def test_read_flag_rejects_malformed_body() -> None:
    with pytest.raises(ValueError, match="JSON object"):
        await _read_flag(_fake_request(body_bytes=b"not json"), "fresh")


@pytest.mark.parametrize("flag", ["fresh", "from_park"])
@pytest.mark.parametrize("value", ["false", "true", 0, 1, None, [], {}])
async def test_invalid_calibration_flag_cannot_start_hardware(flag, value):
    rig = _rig_mock()
    request = _fake_request({flag: value})
    if flag == "fresh":
        response = await handle_measure_viewport_shift(
            request, rig, MagicMock(), MagicMock(), MagicMock()
        )
    else:
        response = await handle_calibrate_arm(request, rig, MagicMock(), MagicMock())

    assert response.status_code == 400
    assert _read_json(response)["message"] == f"{flag} must be a boolean"
    assert rig.mock_calls == []


@pytest.mark.parametrize("flag", ["fresh", "from_park"])
async def test_malformed_calibration_body_cannot_start_hardware(flag):
    rig = _rig_mock()
    request = _fake_request(body_bytes=b"not json")
    if flag == "fresh":
        response = await handle_measure_viewport_shift(
            request, rig, MagicMock(), MagicMock(), MagicMock()
        )
    else:
        response = await handle_calibrate_arm(request, rig, MagicMock(), MagicMock())

    assert response.status_code == 400
    assert rig.mock_calls == []


@pytest.mark.asyncio
async def test_run_blocking_runs_sync_callable() -> None:
    out = await _run_blocking(lambda: "result")

    assert out == "result"


# ---------- handle_measure_viewport_shift ----------


def _viewport_shift() -> ViewportShift:
    return ViewportShift(
        offset_x=0.0,
        offset_y=0.0,
        dpr=3.0,
        screenshot_width=1170,
        screenshot_height=2532,
    )


@pytest.mark.asyncio
async def test_handle_measure_viewport_shift_happy_path(mocker) -> None:
    rig = _rig_mock()
    rig.calibration = SimpleNamespace(viewport_shift=None)
    calib = MagicMock()
    bridge = MagicMock()
    phone = MagicMock()
    vs = _viewport_shift()
    spy = mocker.patch.object(handler, "measure_viewport_shift", return_value=vs)

    resp = await handle_measure_viewport_shift(
        _fake_request(json_obj={"fresh": True}),
        rig,
        calib,
        bridge,
        phone,
    )

    body = _read_json(resp)
    assert body["status"] == "ok"
    assert body == {"status": "ok", **dataclasses.asdict(vs)}
    spy.assert_called_once_with(calib, bridge, fresh=True)
    phone.set_mode.assert_called_once_with("calibrate", phase="screenshot_cal")
    assert rig.calibration.viewport_shift is vs


@pytest.mark.asyncio
async def test_handle_measure_viewport_shift_default_fresh_false(mocker) -> None:
    rig = _rig_mock()
    calib = MagicMock()
    bridge = MagicMock()
    phone = MagicMock()
    vs = _viewport_shift()
    spy = mocker.patch.object(handler, "measure_viewport_shift", return_value=vs)

    await handle_measure_viewport_shift(
        _fake_request(),
        rig,
        calib,
        bridge,
        phone,
    )

    spy.assert_called_once_with(calib, bridge, fresh=False)


@pytest.mark.asyncio
async def test_handle_measure_viewport_shift_returns_err_on_exception(
    mocker, caplog
) -> None:
    rig = _rig_mock()
    calib = MagicMock()
    mocker.patch.object(
        handler,
        "measure_viewport_shift",
        side_effect=RuntimeError("no upload"),
    )

    resp = await handle_measure_viewport_shift(
        _fake_request(json_obj={}),
        rig,
        calib,
        MagicMock(),
        MagicMock(),
    )

    assert resp.status_code == 500
    assert "no upload" in _read_json(resp)["message"]
    assert "Calibration step failed: no upload" in caplog.text
    assert any(record.exc_info for record in caplog.records)


# ---------- handle_calibrate_arm ----------


def _identity_pct_to_grbl() -> np.ndarray:
    return np.array(
        [
            [10.0, 0.0, 0.0],
            [0.0, 20.0, 0.0],
            [0.0, 0.0, 1.0],
        ]
    )


@pytest.mark.asyncio
async def test_handle_calibrate_arm_happy_path(mocker) -> None:
    rig = _rig_mock()
    rig.arm = MagicMock()
    rig.calibration = SimpleNamespace(pct_to_grbl=None)
    calib = MagicMock()
    phone = MagicMock()
    pct_to_grbl = _identity_pct_to_grbl()
    mocker.patch.object(
        handler,
        "calibrate_arm",
        return_value=(pct_to_grbl, 0.01, [(0.1, 0.1, 0)]),
    )
    mocker.patch.object(handler, "TILT_ALIGNED_THRESHOLD", 0.05)

    resp = await handle_calibrate_arm(
        _fake_request(json_obj={}),
        rig,
        calib,
        phone,
    )

    body = _read_json(resp)
    assert body["status"] == "ok"
    assert body["pairs"] == 4  # 1 touch + 3 probe
    assert body["aligned"] is True
    # The affine lands through the one installer (which wires the arm's
    # direction mapping and pins the frame — pinned by test_rig).
    rig.install_arm_calibration.assert_called_once_with(pct_to_grbl)
    rig.park.assert_called_once()
    rig.release.assert_called_once()


@pytest.mark.asyncio
async def test_handle_calibrate_arm_from_park_centers_stylus(mocker) -> None:
    """Auto mode (from_park): with a prior mapping, drive the parked stylus to
    center before calibrating."""
    rig = _rig_mock()
    rig.arm = MagicMock()
    rig.calibration = SimpleNamespace(
        pct_to_grbl=_identity_pct_to_grbl(),
        pct_to_grbl_mm=lambda x, y: (5.0, 6.0),
    )
    mocker.patch.object(
        handler,
        "calibrate_arm",
        return_value=(_identity_pct_to_grbl(), 0.01, []),
    )
    mocker.patch.object(handler, "TILT_ALIGNED_THRESHOLD", 0.05)

    resp = await handle_calibrate_arm(
        _fake_request(json_obj={"from_park": True}),
        rig,
        MagicMock(),
        MagicMock(),
    )

    assert _read_json(resp)["status"] == "ok"
    rig.restore_park_origin.assert_called_once()
    rig.arm.rapid_to.assert_called_once_with(5.0, 6.0)  # screen center
    # No set_origin: the affine is center-rebased so the tip already rests
    # AT the frame zero, and a G92 here would silently void the pin that
    # restore_park_origin just earned — the probe must run pinned so a
    # mid-probe failure park still lands at the true park spot.
    rig.arm.set_origin.assert_not_called()


@pytest.mark.asyncio
async def test_handle_calibrate_arm_from_park_without_bundle_requests_position(
    mocker, caplog
) -> None:
    """A first calibration needs operator positioning before any arm movement."""
    rig = _rig_mock()
    rig.arm = MagicMock()
    rig.calibration = SimpleNamespace(pct_to_grbl=None)
    mocker.patch.object(handler.Calibration, "load", return_value=None)
    cal_arm = mocker.patch.object(handler, "calibrate_arm")

    with caplog.at_level("INFO", logger=handler.__name__):
        resp = await handle_calibrate_arm(
            _fake_request(json_obj={"from_park": True}),
            rig,
            MagicMock(),
            MagicMock(),
        )

    body = _read_json(resp)
    assert resp.status_code == 409
    assert body["status"] == "needs_input"
    assert "No saved arm calibration" in body["message"]
    assert "press Start" in body["message"]
    assert "waiting for stylus positioning" in caplog.text
    cal_arm.assert_not_called()  # never reached the probe
    rig.arm.set_origin.assert_not_called()
    rig.arm.rapid_to.assert_not_called()
    rig.release.assert_called_once()  # lock still released


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("live_affine", "expect_undo"),
    [
        pytest.param(None, True, id="borrowed-affine-undone"),
        pytest.param(_identity_pct_to_grbl(), False, id="live-affine-kept"),
    ],
)
async def test_handle_calibrate_arm_failed_probe_borrow_lifecycle(
    mocker, live_affine, expect_undo
) -> None:
    """A failed probe undoes a BORROWED affine — `mapping_a` must not
    read OK off an affine no probe confirmed (a page reload would skip
    the arm step) — and only AFTER the runner's park, while the frame is
    still pinned to it. An affine already live in the session is kept:
    its `mapping_a: OK` is truthful."""
    rig = _rig_mock()
    rig.arm = MagicMock()
    rig.calibration = SimpleNamespace(
        pct_to_grbl=live_affine, pct_to_grbl_mm=lambda x, y: (5.0, 6.0)
    )
    mocker.patch.object(
        handler.Calibration,
        "load",
        return_value=SimpleNamespace(pct_to_grbl=_identity_pct_to_grbl()),
    )
    mocker.patch.object(
        handler, "calibrate_arm", side_effect=RuntimeError("probe missed")
    )

    resp = await handle_calibrate_arm(
        _fake_request(json_obj={"from_park": True}), rig, MagicMock(), MagicMock()
    )

    assert _read_json(resp)["status"] == "error"
    rig.install_arm_calibration.assert_not_called()
    if expect_undo:
        rig.uninstall_arm_calibration.assert_called_once()
        names = [name for name, *_ in rig.mock_calls]
        assert names.index("park") < names.index("uninstall_arm_calibration")
    else:
        rig.uninstall_arm_calibration.assert_not_called()


@pytest.mark.asyncio
async def test_handle_calibrate_arm_borrow_undone_when_centering_fails(
    mocker,
) -> None:
    """The gap the borrow flag must cover: a failure BETWEEN the borrow
    and the probe (a serial error during the centering move) must undo
    the borrow too — the flag is set at the borrow, not on the motion
    helper's return."""
    rig = _rig_mock()
    rig.arm = MagicMock()
    rig.arm.rapid_to.side_effect = RuntimeError("serial gone mid-move")
    rig.calibration = SimpleNamespace(
        pct_to_grbl=None, pct_to_grbl_mm=lambda x, y: (5.0, 6.0)
    )
    mocker.patch.object(
        handler.Calibration,
        "load",
        return_value=SimpleNamespace(pct_to_grbl=_identity_pct_to_grbl()),
    )
    cal_arm = mocker.patch.object(handler, "calibrate_arm")

    resp = await handle_calibrate_arm(
        _fake_request(json_obj={"from_park": True}), rig, MagicMock(), MagicMock()
    )

    assert _read_json(resp)["status"] == "error"
    cal_arm.assert_not_called()  # never reached the probe
    rig.uninstall_arm_calibration.assert_called_once()


@pytest.mark.asyncio
async def test_handle_calibrate_arm_confirmed_affine_survives_park_failure(
    mocker,
) -> None:
    """The borrow is repaid at install: a probe that SUCCEEDED must keep
    its confirmed affine even when the closing park then fails — the
    step errors (redo the park situation), but the calibration is real
    and must not be wiped."""
    rig = _rig_mock()
    rig.arm = MagicMock()
    rig.park.side_effect = RuntimeError("serial gone at park")
    rig.calibration = SimpleNamespace(
        pct_to_grbl=None, pct_to_grbl_mm=lambda x, y: (5.0, 6.0)
    )
    mocker.patch.object(
        handler.Calibration,
        "load",
        return_value=SimpleNamespace(pct_to_grbl=_identity_pct_to_grbl()),
    )
    mocker.patch.object(
        handler,
        "calibrate_arm",
        return_value=(_identity_pct_to_grbl(), 0.01, []),
    )

    resp = await handle_calibrate_arm(
        _fake_request(json_obj={"from_park": True}), rig, MagicMock(), MagicMock()
    )

    assert _read_json(resp)["status"] == "error"
    rig.install_arm_calibration.assert_called_once()  # the probe confirmed it
    rig.uninstall_arm_calibration.assert_not_called()  # ...so nothing is undone


@pytest.mark.asyncio
async def test_handle_calibrate_arm_arm_not_connected() -> None:
    rig = _rig_mock()
    rig.arm = None

    resp = await handle_calibrate_arm(
        _fake_request(json_obj={}),
        rig,
        MagicMock(),
        MagicMock(),
    )

    # Precondition failures are client state, not server faults.
    assert resp.status_code == 409
    assert "Arm not connected" in _read_json(resp)["message"]


@pytest.mark.asyncio
async def test_handle_calibrate_arm_releases_on_failure(mocker) -> None:
    rig = _rig_mock()
    rig.arm = MagicMock()
    rig.calibration = SimpleNamespace(pct_to_grbl=None)
    mocker.patch.object(
        handler,
        "calibrate_arm",
        side_effect=RuntimeError("probe failed"),
    )

    resp = await handle_calibrate_arm(
        _fake_request(json_obj={}),
        rig,
        MagicMock(),
        MagicMock(),
    )

    assert resp.status_code == 500
    rig.release.assert_called_once()


# ---------- handle_calibrate_camera_frame ----------


@pytest.mark.asyncio
async def test_handle_calibrate_camera_frame_happy_path(mocker) -> None:
    rig = _rig_mock()
    rig.cam = MagicMock()
    rig.calibration = SimpleNamespace()
    mocker.patch.object(
        handler,
        "calibrate_camera_frame",
        return_value={"rotation": 0, "issues": []},
    )

    resp = await handle_calibrate_camera_frame(
        _fake_request(json_obj={}),
        rig,
        MagicMock(),
    )

    body = _read_json(resp)
    assert body["rotation"] == 0
    assert rig.calibration.cam_rotation == 0
    assert rig.cam.rotation == 0
    rig.park.assert_called_once()
    rig.release.assert_called_once()


@pytest.mark.asyncio
async def test_handle_calibrate_camera_frame_camera_not_connected() -> None:
    rig = _rig_mock()
    rig.cam = None

    resp = await handle_calibrate_camera_frame(
        _fake_request(),
        rig,
        MagicMock(),
    )

    # Precondition failures are client state, not server faults.
    assert resp.status_code == 409
    assert "Camera not connected" in _read_json(resp)["message"]


@pytest.mark.asyncio
async def test_handle_calibrate_camera_frame_releases_on_failure(mocker) -> None:
    rig = _rig_mock()
    rig.cam = MagicMock()
    mocker.patch.object(
        handler,
        "calibrate_camera_frame",
        side_effect=RuntimeError("no markers"),
    )

    resp = await handle_calibrate_camera_frame(
        _fake_request(),
        rig,
        MagicMock(),
    )

    assert resp.status_code == 500
    rig.release.assert_called_once()


# ---------- handle_compute_camera_mapping ----------


@pytest.mark.asyncio
async def test_handle_compute_camera_mapping_happy_path(mocker) -> None:
    rig = _rig_mock()
    rig.cam = MagicMock()
    rig.calibration = SimpleNamespace(effective_rotation=lambda: 0)
    pct_to_cam = np.eye(2, 3)
    mocker.patch.object(
        handler,
        "compute_camera_mapping",
        return_value=(pct_to_cam, (1920, 1080), 137.0),
    )

    resp = await handle_compute_camera_mapping(
        _fake_request(),
        rig,
        MagicMock(),
    )

    body = _read_json(resp)
    assert body["ok"] is True
    assert body["dots"] == 15
    assert body["cam_size"] == [1920, 1080]
    assert body["cam_focus"] == 137.0
    assert rig.calibration.pct_to_cam is pct_to_cam
    assert rig.calibration.cam_size == (1920, 1080)
    assert rig.calibration.cam_focus == 137.0


@pytest.mark.asyncio
async def test_handle_compute_camera_mapping_camera_not_connected() -> None:
    rig = _rig_mock()
    rig.cam = None

    resp = await handle_compute_camera_mapping(
        _fake_request(),
        rig,
        MagicMock(),
    )

    # Precondition failures are client state, not server faults.
    assert resp.status_code == 409
    assert "Camera not connected" in _read_json(resp)["message"]


@pytest.mark.asyncio
async def test_handle_compute_camera_mapping_releases_on_failure(mocker) -> None:
    rig = _rig_mock()
    rig.cam = MagicMock()
    rig.calibration = SimpleNamespace(effective_rotation=lambda: 0)
    mocker.patch.object(
        handler,
        "compute_camera_mapping",
        side_effect=RuntimeError("dots missing"),
    )

    resp = await handle_compute_camera_mapping(
        _fake_request(),
        rig,
        MagicMock(),
    )

    assert resp.status_code == 500
    rig.release.assert_called_once()


# ---------- handle_validate_calibration ----------


@pytest.mark.asyncio
async def test_handle_validate_calibration_happy_path_and_persists(mocker) -> None:
    rig = _rig_mock()
    rig.arm = MagicMock()
    cal = MagicMock()
    cal.transforms_ready = True
    cal.pct_to_grbl = _identity_pct_to_grbl()
    cal.pct_to_cam = np.eye(3)
    cal.cam_size = (1920, 1080)
    cal.effective_rotation.return_value = 0
    rig.calibration = cal
    calib = MagicMock()
    calib.screen_dimension = (390, 844)
    phone = MagicMock()

    results = [{"passed": True}, {"passed": True}, {"passed": False}]
    mocker.patch.object(handler, "validate_calibration", return_value=results)

    resp = await handle_validate_calibration(
        _fake_request(),
        rig,
        calib,
        phone,
    )

    body = _read_json(resp)
    assert body["passed"] == 2
    assert body["total"] == 3
    assert body["calibrated"] is True
    phone.set_mode.assert_called_once_with("bridge")
    cal.save.assert_called_once()
    assert cal.screen_dimension == (390, 844)


@pytest.mark.asyncio
async def test_handle_validate_calibration_does_not_save_when_not_calibrated(
    mocker,
) -> None:
    rig = _rig_mock()
    rig.arm = MagicMock()
    cal = MagicMock()
    cal.transforms_ready = True
    cal.pct_to_grbl = _identity_pct_to_grbl()
    cal.pct_to_cam = np.eye(3)
    cal.cam_size = (1920, 1080)
    cal.effective_rotation.return_value = 0
    rig.calibration = cal
    phone = MagicMock()
    mocker.patch.object(
        handler,
        "validate_calibration",
        return_value=[{"passed": False}, {"passed": True}],
    )

    resp = await handle_validate_calibration(
        _fake_request(),
        rig,
        MagicMock(),
        phone,
    )

    body = _read_json(resp)
    assert body["calibrated"] is False
    cal.save.assert_not_called()
    phone.set_mode.assert_not_called()


@pytest.mark.asyncio
async def test_handle_validate_calibration_arm_not_connected() -> None:
    rig = _rig_mock()
    rig.arm = None

    resp = await handle_validate_calibration(
        _fake_request(),
        rig,
        MagicMock(),
        MagicMock(),
    )

    # Precondition failures are client state, not server faults.
    assert resp.status_code == 409
    assert "Arm not connected" in _read_json(resp)["message"]


@pytest.mark.asyncio
async def test_handle_validate_calibration_requires_transforms_ready() -> None:
    rig = _rig_mock()
    rig.arm = MagicMock()
    cal = MagicMock()
    cal.transforms_ready = False
    rig.calibration = cal

    resp = await handle_validate_calibration(
        _fake_request(),
        rig,
        MagicMock(),
        MagicMock(),
    )

    # Precondition failures are client state, not server faults.
    assert resp.status_code == 409
    assert "arm calibration" in _read_json(resp)["message"]


# ---------- handle_trace_edge ----------


@pytest.mark.asyncio
async def test_handle_trace_edge_happy_path(mocker) -> None:
    rig = _rig_mock()
    rig.transforms = MagicMock()  # truthy → calibrated
    phone = MagicMock()
    spy = mocker.patch(
        "physiclaw.core.calibration.calibrate.trace_screen_edge",
    )

    resp = await handle_trace_edge(_fake_request(), rig, phone)

    assert _read_json(resp) == {"status": "ok", "ok": True}
    phone.set_mode.assert_called_once_with("bridge")
    rig.park.assert_called_once()
    spy.assert_called_once()


@pytest.mark.asyncio
async def test_handle_trace_edge_uncalibrated_returns_error() -> None:
    rig = _rig_mock()
    rig.transforms = None

    resp = await handle_trace_edge(_fake_request(), rig, MagicMock())

    # Precondition failures are client state, not server faults.
    assert resp.status_code == 409
    assert "Not calibrated" in _read_json(resp)["message"]


@pytest.mark.asyncio
async def test_handle_trace_edge_releases_on_failure(mocker) -> None:
    rig = _rig_mock()
    rig.transforms = MagicMock()
    mocker.patch(
        "physiclaw.core.calibration.calibrate.trace_screen_edge",
        side_effect=RuntimeError("arm off"),
    )

    resp = await handle_trace_edge(_fake_request(), rig, MagicMock())

    assert resp.status_code == 500
    rig.release.assert_called_once()


# ---------- handle_show_assistive_touch ----------


@pytest.mark.asyncio
async def test_handle_show_assistive_touch_happy_path(mocker) -> None:
    rig = _rig_mock()
    rig.assistive_touch.at_screen = (0.1, 0.2)
    calib = SimpleNamespace(viewport_shift=_viewport_shift())
    phone = MagicMock()
    mocker.patch.object(handler, "generate_nonce", return_value=[1, 0, 1])

    resp = await handle_show_assistive_touch(
        _fake_request(),
        rig,
        calib,
        phone,
    )

    body = _read_json(resp)
    assert body["status"] == "ok"
    assert body["at_screen"] == [0.1, 0.2]
    assert body["nonce_count"] == 3
    rig.assistive_touch.compute_at_screen_pos.assert_called_once_with(
        calib.viewport_shift,
    )
    phone.set_mode.assert_called_once_with(
        "calibrate",
        phase="assistive_touch",
        nonce_bits=[1, 0, 1],
    )


@pytest.mark.asyncio
async def test_handle_show_assistive_touch_requires_viewport_shift() -> None:
    rig = _rig_mock()
    calib = SimpleNamespace(viewport_shift=None)

    resp = await handle_show_assistive_touch(
        _fake_request(),
        rig,
        calib,
        MagicMock(),
    )

    # Precondition failures are client state (409) — same contract as
    # every other calibration precheck.
    assert resp.status_code == 409
    assert "Viewport shift not measured" in _read_json(resp)["message"]


# ---------- handle_verify_assistive_touch ----------


@pytest.mark.asyncio
async def test_handle_verify_assistive_touch_happy_path(mocker) -> None:
    rig = _rig_mock()
    rig.arm = MagicMock()
    rig.calibration = SimpleNamespace(pct_to_grbl=_identity_pct_to_grbl())
    rig.assistive_touch.at_screen = (0.1, 0.2)
    spy = mocker.patch.object(
        handler,
        "verify_assistive_touch",
        return_value={"verified": True},
    )

    resp = await handle_verify_assistive_touch(
        _fake_request(),
        rig,
        MagicMock(),
        MagicMock(),
        MagicMock(),
    )

    assert _read_json(resp) == {"status": "ok", "verified": True}
    spy.assert_called_once()
    rig.release.assert_called_once()


@pytest.mark.asyncio
async def test_handle_verify_assistive_touch_arm_not_connected() -> None:
    rig = _rig_mock()
    rig.arm = None

    resp = await handle_verify_assistive_touch(
        _fake_request(),
        rig,
        MagicMock(),
        MagicMock(),
        MagicMock(),
    )

    # Precondition failures are client state, not server faults.
    assert resp.status_code == 409
    assert "Arm not connected" in _read_json(resp)["message"]


@pytest.mark.asyncio
async def test_handle_verify_assistive_touch_requires_pct_to_grbl() -> None:
    rig = _rig_mock()
    rig.arm = MagicMock()
    rig.calibration = SimpleNamespace(pct_to_grbl=None)

    resp = await handle_verify_assistive_touch(
        _fake_request(),
        rig,
        MagicMock(),
        MagicMock(),
        MagicMock(),
    )

    # Precondition failures are client state, not server faults.
    assert resp.status_code == 409
    assert "arm calibration" in _read_json(resp)["message"]


@pytest.mark.asyncio
async def test_handle_verify_assistive_touch_requires_at_show() -> None:
    rig = _rig_mock()
    rig.arm = MagicMock()
    rig.calibration = SimpleNamespace(pct_to_grbl=_identity_pct_to_grbl())
    rig.assistive_touch.at_screen = None  # show step not run

    resp = await handle_verify_assistive_touch(
        _fake_request(),
        rig,
        MagicMock(),
        MagicMock(),
        MagicMock(),
    )

    # Precondition failures are client state, not server faults.
    assert resp.status_code == 409
    assert "assistive-touch/show" in _read_json(resp)["message"]


def _real_ready_rig(mocker):
    arm, cam = MagicMock(), MagicMock()
    mocker.patch.object(rig_mod, "StylusArm", return_value=arm)
    mocker.patch.object(rig_mod, "Camera", return_value=cam)
    rig = rig_mod.HardwareRig()
    rig.connect_arm()
    rig.connect_camera(0)
    rig.install_arm_calibration(np.eye(2, 3))
    rig.calibration.pct_to_cam = np.eye(2, 3)
    rig.calibration.cam_size = (100, 100)
    rig.calibration.cam_focus = 42
    rig.calibration.cam_rotation = -1
    rig.mark_ready()
    calib = CalibrationState()
    calib.viewport_shift = ViewportShift(0, 0, 1, 100, 100)
    calib.screen_dimension = {"viewport_width": 100, "viewport_height": 100}
    rig.calibration.viewport_shift = calib.viewport_shift
    rig.calibration.screen_dimension = calib.screen_dimension
    return rig, calib


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route, step",
    [
        (handle_calibrate_camera_frame, "calibrate_camera_frame"),
        (handle_compute_camera_mapping, "compute_camera_mapping"),
    ],
)
async def test_regression_camera_recalibration_failure_invalidates_old_mapping(
    mocker, route, step
) -> None:
    rig, calib = _real_ready_rig(mocker)
    previous_arm_mapping = rig.calibration.pct_to_grbl
    mocker.patch.object(handler, step, side_effect=RuntimeError("measurement failed"))

    response = await route(_fake_request(), rig, calib)

    assert response.status_code == 500
    assert rig.calibration.pct_to_cam is None
    assert rig.calibration.cam_size is None
    assert rig.calibration.cam_focus is None
    assert rig.calibration.pct_to_grbl is previous_arm_mapping
    assert rig.origin_pinned is True
    assert rig.ready is False


@pytest.mark.asyncio
async def test_camera_mapping_missing_prerequisite_preserves_existing_mapping(
    mocker,
) -> None:
    rig, calib = _real_ready_rig(mocker)
    previous = rig.calibration.pct_to_cam
    calib.viewport_shift = None
    step = mocker.patch.object(handler, "compute_camera_mapping")

    response = await handle_compute_camera_mapping(_fake_request(), rig, calib)

    assert response.status_code == 409
    assert rig.calibration.pct_to_cam is previous
    assert rig.ready is True
    step.assert_not_called()


@pytest.mark.asyncio
async def test_successful_camera_remapping_requires_readiness_confirmation(mocker):
    rig, calib = _real_ready_rig(mocker)
    mocker.patch.object(
        handler, "compute_camera_mapping", return_value=(np.eye(2, 3), (100, 100), 43)
    )

    response = await handle_compute_camera_mapping(_fake_request(), rig, calib)

    assert response.status_code == 200
    assert rig.hardware_ready is True
    assert rig.calibration.cam_focus == 43
    assert rig.ready is False


@pytest.mark.asyncio
async def test_regression_camera_frame_change_cannot_reuse_old_fit(mocker) -> None:
    rig, calib = _real_ready_rig(mocker)
    mocker.patch.object(handler, "calibrate_camera_frame", return_value={"rotation": 2})

    response = await handle_calibrate_camera_frame(_fake_request(), rig, calib)

    assert response.status_code == 200
    assert rig.cam.rotation == 2
    assert rig.calibration.cam_rotation == 2
    assert rig.transforms is None
    assert rig.ready is False


@pytest.mark.asyncio
async def test_regression_accurate_fallback_taps_do_not_save_calibration(
    mocker,
) -> None:
    mocker.patch.object(validate_mod.time, "sleep")
    mocker.patch.object(validate_mod.random, "random", return_value=0.5)
    rig, calib = _real_ready_rig(mocker)
    rig.cam.raw_frame.return_value = None
    rig.arm.solenoid.tap.side_effect = lambda *_: calib.report_touch(
        {"x": 0.5, "y": 0.5}
    )
    phone = PageState(BridgeState(), calib)
    save = mocker.patch.object(rig.calibration, "save")

    response = await handle_validate_calibration(_fake_request(), rig, calib, phone)

    result = _read_json(response)
    assert response.status_code == 200
    assert result["calibrated"] is False
    assert result["passed"] == 0
    assert [r["error"] for r in result["results"]] == [0] * 5
    save.assert_not_called()


def _active_setup(mocker):
    rig, cal = _real_ready_rig(mocker)
    bridge = BridgeState()
    phone = PageState(bridge, cal)
    phone.set_mode("calibrate", phase="grid")
    cal.report_touch({"x": 0.5, "y": 0.5})
    rig.assistive_touch.compute_at_screen_pos(cal.viewport_shift)
    return rig, cal, bridge, phone


async def _invoke_setup(route, rig, cal, bridge, phone, body=None):
    request = _fake_request(body)
    calls = {
        "arm": (handle_calibrate_arm, (request, rig, cal, phone)),
        "viewport": (handle_measure_viewport_shift, (request, rig, cal, bridge, phone)),
        "show": (handle_show_assistive_touch, (request, rig, cal, phone)),
        "camera": (handle_compute_camera_mapping, (request, rig, cal)),
        "validate": (handle_validate_calibration, (request, rig, cal, phone)),
        "trace": (handle_trace_edge, (request, rig, phone)),
        "verify": (handle_verify_assistive_touch, (request, rig, cal, bridge, phone)),
    }
    function, args = calls[route]
    return await function(*args)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["arm", "viewport", "show"])
async def test_regression_busy_setup_preserves_active_page_and_touches(mocker, route):
    rig, cal, bridge, phone = _active_setup(mocker)
    previous = rig.calibration.pct_to_grbl
    rig.acquire()

    try:
        response = await _invoke_setup(route, rig, cal, bridge, phone)
    finally:
        rig.release()

    assert response.status_code == 500
    assert "busy" in _read_json(response)["message"]
    assert cal.phase == "grid"
    assert cal.touches == [{"x": 0.5, "y": 0.5}]
    assert rig.calibration.pct_to_grbl is previous
    assert rig.ready is True
    rig.arm.rapid_to.assert_not_called()
    rig.arm.set_origin.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route, body",
    [
        ("arm", {}),
        ("arm", {"from_park": True}),
        ("camera", {}),
        ("validate", {}),
        ("verify", {}),
        ("show", {}),
    ],
)
async def test_regression_missing_viewport_never_moves_or_changes_page(
    mocker, route, body
):
    rig, cal, bridge, phone = _active_setup(mocker)
    previous = rig.calibration.pct_to_grbl
    cal.viewport_shift = None

    response = await _invoke_setup(route, rig, cal, bridge, phone, body)

    assert response.status_code == 409
    assert "Viewport shift" in _read_json(response)["message"]
    assert cal.phase == "grid"
    assert rig.calibration.pct_to_grbl is previous
    assert rig.ready is True
    rig.arm.rapid_to.assert_not_called()
    rig.arm.set_origin.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["arm", "viewport", "camera", "validate"])
async def test_missing_screen_dimensions_take_precedence_over_busy_setup(mocker, route):
    rig, cal, bridge, phone = _active_setup(mocker)
    cal.screen_dimension = None
    rig.acquire()

    try:
        response = await _invoke_setup(route, rig, cal, bridge, phone)
    finally:
        rig.release()

    assert response.status_code == 409
    assert "Screen dimension" in _read_json(response)["message"]
    assert cal.phase == "grid"
    assert cal.touches == [{"x": 0.5, "y": 0.5}]
    assert rig.ready is True
    rig.arm.rapid_to.assert_not_called()
    rig.arm.set_origin.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["validate", "trace", "verify"])
async def test_regression_verification_refuses_reconnected_unpinned_arm(mocker, route):
    rig, cal, bridge, phone = _active_setup(mocker)
    rig.connect_arm()

    response = await _invoke_setup(route, rig, cal, bridge, phone)

    assert response.status_code == 409
    assert "not pinned" in _read_json(response)["message"]
    rig.arm.rapid_to.assert_not_called()
    rig.arm.tap.assert_not_called()
    rig.arm.solenoid.tap.assert_not_called()


@pytest.mark.asyncio
async def test_trace_refuses_arm_missing_after_failed_reconnection(mocker):
    rig, cal, bridge, phone = _active_setup(mocker)
    arm = rig.arm
    arm.setup.side_effect = RuntimeError("serial setup failed")
    with pytest.raises(RuntimeError, match="serial setup failed"):
        rig.connect_arm()

    response = await _invoke_setup("trace", rig, cal, bridge, phone)

    assert response.status_code == 409
    assert "Arm not connected" in _read_json(response)["message"]
    assert cal.phase == "grid"
    arm.rapid_to.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["validate", "trace", "verify"])
async def test_verification_rechecks_origin_after_acquiring_lock(mocker, route):
    rig, cal, bridge, phone = _active_setup(mocker)
    locked = rig.locked

    @contextmanager
    def reconnect_before_acquire():
        rig.connect_arm()
        with locked():
            yield

    mocker.patch.object(rig, "locked", side_effect=reconnect_before_acquire)

    response = await _invoke_setup(route, rig, cal, bridge, phone)

    assert response.status_code == 409
    assert "not pinned" in _read_json(response)["message"]
    rig.arm.rapid_to.assert_not_called()
    rig.arm.solenoid.tap.assert_not_called()


@pytest.mark.asyncio
async def test_regression_manual_probe_starts_with_new_origin_and_no_stale_mapping(
    mocker,
):
    rig, cal, bridge, phone = _active_setup(mocker)
    events = []

    def reset_origin():
        rig.assert_locked()
        assert rig.calibration.pct_to_grbl is None
        assert rig.origin_pinned is False
        assert rig.ready is False
        events.append("origin")

    def probe(*_):
        assert events == ["origin"]
        events.append("probe")
        return np.eye(2, 3), 0.0, []

    rig.arm.set_origin.side_effect = reset_origin
    mocker.patch.object(handler, "calibrate_arm", side_effect=probe)

    response = await _invoke_setup("arm", rig, cal, bridge, phone)

    assert response.status_code == 200
    assert events == ["origin", "probe"]
    assert rig.origin_pinned is True
    assert rig.ready is False
    rig.arm.rapid_to.assert_called_once_with(-0.1, -0.05)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["origin", "probe"])
async def test_failed_manual_calibration_cannot_park_with_old_coordinates(
    mocker, stage
):
    rig, cal, bridge, phone = _active_setup(mocker)
    failures = {
        "origin": (rig.arm, "set_origin"),
        "probe": (handler, "calibrate_arm"),
    }
    target, method = failures[stage]
    mocker.patch.object(target, method, side_effect=RuntimeError("device failed"))

    response = await _invoke_setup("arm", rig, cal, bridge, phone)

    assert response.status_code == 500
    assert rig.calibration.pct_to_grbl is None
    assert rig.origin_pinned is False
    assert rig.ready is False
    rig.arm.rapid_to.assert_not_called()


@pytest.mark.asyncio
async def test_auto_failed_borrow_parks_before_discarding_provisional_coordinates(
    mocker,
):
    rig, cal, bridge, phone = _active_setup(mocker)
    prior = handler.Calibration(pct_to_grbl=rig.calibration.pct_to_grbl.copy())
    rig.uninstall_arm_calibration()
    mocker.patch.object(handler.Calibration, "load", return_value=prior)
    mocker.patch.object(
        handler, "calibrate_arm", side_effect=RuntimeError("probe failed")
    )

    response = await _invoke_setup("arm", rig, cal, bridge, phone, {"from_park": True})

    assert response.status_code == 500
    assert rig.arm.rapid_to.call_args_list == [
        mocker.call(0.5, 0.5),
        mocker.call(-0.1, -0.05),
    ]
    assert rig.calibration.pct_to_grbl is None
    assert rig.origin_pinned is False
    rig.arm.set_origin.assert_not_called()


@pytest.mark.asyncio
async def test_changed_viewport_retires_all_dependent_geometry_after_parking(mocker):
    rig, cal, bridge, phone = _active_setup(mocker)
    measured = dataclasses.replace(cal.viewport_shift, offset_y=20)
    mocker.patch.object(handler, "measure_viewport_shift", return_value=measured)

    response = await _invoke_setup("viewport", rig, cal, bridge, phone)

    assert response.status_code == 200
    assert cal.viewport_shift is measured
    assert rig.calibration.viewport_shift is measured
    assert rig.calibration.pct_to_grbl is None
    assert rig.calibration.pct_to_cam is None
    assert rig.calibration.cam_size is None
    assert rig.calibration.cam_focus is None
    assert rig.assistive_touch.at_screen is None
    assert rig.assistive_touch.at_radius_screen is None
    assert rig.origin_pinned is False
    assert rig.ready is False
    rig.arm.rapid_to.assert_called_once_with(-0.1, -0.05)


@pytest.mark.asyncio
async def test_unchanged_viewport_preserves_existing_calibration(mocker):
    rig, cal, bridge, phone = _active_setup(mocker)
    previous_arm = rig.calibration.pct_to_grbl
    previous_camera = rig.calibration.pct_to_cam
    previous_at = rig.assistive_touch.at_screen
    mocker.patch.object(
        handler, "measure_viewport_shift", return_value=cal.viewport_shift
    )

    response = await _invoke_setup("viewport", rig, cal, bridge, phone)

    assert response.status_code == 200
    assert rig.calibration.pct_to_grbl is previous_arm
    assert rig.calibration.pct_to_cam is previous_camera
    assert rig.assistive_touch.at_screen is previous_at
    assert rig.origin_pinned is True
    assert rig.ready is True
    rig.arm.rapid_to.assert_not_called()


@pytest.mark.asyncio
async def test_viewport_measurement_failure_preserves_prior_state_without_motion(
    mocker,
):
    rig, cal, bridge, phone = _active_setup(mocker)
    previous = cal.viewport_shift
    previous_arm = rig.calibration.pct_to_grbl
    mocker.patch.object(
        handler, "measure_viewport_shift", side_effect=RuntimeError("no upload")
    )

    response = await _invoke_setup("viewport", rig, cal, bridge, phone)

    assert response.status_code == 500
    assert cal.viewport_shift is previous
    assert rig.calibration.viewport_shift is previous
    assert rig.calibration.pct_to_grbl is previous_arm
    assert rig.origin_pinned is True
    assert rig.ready is True
    rig.arm.rapid_to.assert_not_called()


@pytest.mark.asyncio
async def test_viewport_park_failure_still_invalidates_stale_geometry(mocker):
    rig, cal, bridge, phone = _active_setup(mocker)
    measured = dataclasses.replace(cal.viewport_shift, offset_y=20)
    mocker.patch.object(handler, "measure_viewport_shift", return_value=measured)
    rig.arm.rapid_to.side_effect = RuntimeError("park failed")

    response = await _invoke_setup("viewport", rig, cal, bridge, phone)

    assert response.status_code == 500
    assert cal.viewport_shift is measured
    assert rig.calibration.viewport_shift is measured
    assert rig.transforms is None
    assert rig.origin_pinned is False
    assert rig.ready is False
    assert rig.assistive_touch.at_screen is None
    rig.arm.rapid_to.assert_called_once()


@pytest.mark.asyncio
async def test_show_assistive_touch_failure_does_not_move_arm(mocker):
    rig, cal, bridge, phone = _active_setup(mocker)
    mocker.patch.object(phone, "set_mode", side_effect=RuntimeError("page failed"))

    response = await _invoke_setup("show", rig, cal, bridge, phone)

    assert response.status_code == 500
    rig.arm.rapid_to.assert_not_called()


@pytest.mark.asyncio
async def test_manual_page_failure_cannot_park_using_hand_positioned_stale_frame(
    mocker,
):
    rig, cal, bridge, phone = _active_setup(mocker)
    mocker.patch.object(phone, "set_mode", side_effect=RuntimeError("page failed"))

    response = await _invoke_setup("arm", rig, cal, bridge, phone)

    assert response.status_code == 500
    assert rig.calibration.pct_to_grbl is None
    assert rig.origin_pinned is False
    assert rig.ready is False
    rig.arm.rapid_to.assert_not_called()
    rig.arm.set_origin.assert_not_called()


@pytest.mark.asyncio
async def test_missing_auto_calibration_preserves_current_page_and_touches(mocker):
    rig, cal, bridge, phone = _active_setup(mocker)
    rig.uninstall_arm_calibration()
    mocker.patch.object(handler.Calibration, "load", return_value=None)

    response = await _invoke_setup("arm", rig, cal, bridge, phone, {"from_park": True})

    assert response.status_code == 409
    assert _read_json(response)["status"] == "needs_input"
    assert cal.phase == "grid"
    assert cal.touches == [{"x": 0.5, "y": 0.5}]
    rig.arm.rapid_to.assert_not_called()
