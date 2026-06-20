"""Tests for `physiclaw.core.server.warm_start` — focused on
`wait_for_port` (testable) and `try_resume` happy-path / early-return
branches with elaborate mocking. The `_sanity` helper is integration-
only and lives behind hardware fakes.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from physiclaw.core.server import warm_start
from physiclaw.core.server.warm_start import wait_for_port


# ---------- wait_for_port ----------


def test_wait_for_port_returns_true_on_immediate_connect(mocker) -> None:
    fake_sock = MagicMock()
    fake_sock.__enter__ = MagicMock(return_value=fake_sock)
    fake_sock.__exit__ = MagicMock(return_value=None)
    fake_sock.connect.return_value = None  # success
    mocker.patch.object(warm_start.socket, "socket", return_value=fake_sock)

    assert wait_for_port("127.0.0.1", 8048, timeout=1.0) is True


def test_wait_for_port_returns_false_on_timeout(mocker) -> None:
    fake_sock = MagicMock()
    fake_sock.__enter__ = MagicMock(return_value=fake_sock)
    fake_sock.__exit__ = MagicMock(return_value=None)
    fake_sock.connect.side_effect = OSError("connection refused")
    mocker.patch.object(warm_start.socket, "socket", return_value=fake_sock)
    mocker.patch.object(warm_start.time, "sleep")
    # Force the deadline immediately by faking monotonic.
    times = iter([0.0, 100.0])  # deadline=1.0; first check passes, second exits
    mocker.patch.object(
        warm_start.time, "monotonic", side_effect=lambda: next(times)
    )

    assert wait_for_port("127.0.0.1", 8048, timeout=1.0) is False


def test_wait_for_port_uses_loopback_when_host_is_wildcard(mocker) -> None:
    fake_sock = MagicMock()
    fake_sock.__enter__ = MagicMock(return_value=fake_sock)
    fake_sock.__exit__ = MagicMock(return_value=None)
    fake_sock.connect.return_value = None
    mocker.patch.object(warm_start.socket, "socket", return_value=fake_sock)

    wait_for_port("0.0.0.0", 8048, timeout=1.0)

    fake_sock.connect.assert_called_once_with(("127.0.0.1", 8048))


def test_wait_for_port_uses_loopback_when_host_is_empty(mocker) -> None:
    fake_sock = MagicMock()
    fake_sock.__enter__ = MagicMock(return_value=fake_sock)
    fake_sock.__exit__ = MagicMock(return_value=None)
    fake_sock.connect.return_value = None
    mocker.patch.object(warm_start.socket, "socket", return_value=fake_sock)

    wait_for_port("", 8048, timeout=1.0)

    fake_sock.connect.assert_called_once_with(("127.0.0.1", 8048))


def test_wait_for_port_uses_named_host_unchanged(mocker) -> None:
    fake_sock = MagicMock()
    fake_sock.__enter__ = MagicMock(return_value=fake_sock)
    fake_sock.__exit__ = MagicMock(return_value=None)
    fake_sock.connect.return_value = None
    mocker.patch.object(warm_start.socket, "socket", return_value=fake_sock)

    wait_for_port("api.host", 8048, timeout=1.0)

    fake_sock.connect.assert_called_once_with(("api.host", 8048))


# ---------- try_resume: early-return branches ----------


def test_try_resume_returns_false_when_no_bundle(
    mocker, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    mocker.patch(
        "physiclaw.core.calibration.state.Calibration.load", return_value=None
    )

    with caplog.at_level(logging.ERROR, logger="physiclaw.core.server.warm_start"):
        result = warm_start.try_resume(cam_index_override=None)

    assert result is False
    assert any(
        "no calibration bundle on disk" in r.getMessage()
        for r in caplog.records
    )


def test_try_resume_returns_false_when_bundle_incomplete(
    mocker, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    fake_cal = MagicMock()
    fake_cal.complete = False
    fake_cal.viewport_shift = None
    fake_cal.screen_dimension = None
    fake_cal.cam_rotation = 0
    mocker.patch(
        "physiclaw.core.calibration.state.Calibration.load",
        return_value=fake_cal,
    )
    fake_app = MagicMock()
    mocker.patch("physiclaw.core.server.app.physiclaw", fake_app)
    mocker.patch("physiclaw.core.server.app._calib", MagicMock())
    mocker.patch("physiclaw.core.server.app._phone", MagicMock())

    with caplog.at_level(logging.ERROR, logger="physiclaw.core.server.warm_start"):
        result = warm_start.try_resume(cam_index_override=None)

    assert result is False
    assert any(
        "bundle on disk is incomplete" in r.getMessage()
        for r in caplog.records
    )


def test_try_resume_returns_false_when_hardware_connect_raises(
    mocker, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    fake_cal = MagicMock()
    fake_cal.complete = True
    fake_cal.viewport_shift = None
    fake_cal.screen_dimension = None
    fake_cal.cam_index = 0
    fake_cal.cam_rotation = 0
    mocker.patch(
        "physiclaw.core.calibration.state.Calibration.load",
        return_value=fake_cal,
    )
    fake_app = MagicMock()
    fake_app.connect_arm.side_effect = RuntimeError("port unavailable")
    mocker.patch("physiclaw.core.server.app.physiclaw", fake_app)
    mocker.patch("physiclaw.core.server.app._calib", MagicMock())
    mocker.patch("physiclaw.core.server.app._phone", MagicMock())

    with caplog.at_level(logging.ERROR, logger="physiclaw.core.server.warm_start"):
        result = warm_start.try_resume(cam_index_override=None)

    assert result is False
    assert any(
        "hardware reconnect failed: port unavailable" in r.getMessage()
        for r in caplog.records
    )


# ---------- try_resume: post-connect flow ----------


pytestmark_phase5 = [pytest.mark.integration]


def _ready_bundle() -> MagicMock:
    """A complete-and-loaded Calibration mock."""
    cal = MagicMock()
    cal.complete = True
    cal.viewport_shift = MagicMock()
    cal.screen_dimension = (390, 844)
    cal.cam_index = 1
    cal.cam_rotation = 0
    cal.pct_to_grbl = MagicMock()
    cal.pct_to_cam = MagicMock()
    cal.cam_size = (1920, 1080)
    cal.effective_rotation.return_value = 0
    return cal


def _ready_app(cal) -> MagicMock:
    app = MagicMock()
    app.calibration = cal
    app.assistive_touch = MagicMock()
    return app


@pytest.mark.integration
def test_try_resume_succeeds_on_clean_path(mocker) -> None:
    cal = _ready_bundle()
    app = _ready_app(cal)
    mocker.patch(
        "physiclaw.core.calibration.state.Calibration.load", return_value=cal,
    )
    mocker.patch("physiclaw.core.server.app.physiclaw", app)
    fake_calib_state = MagicMock()
    mocker.patch("physiclaw.core.server.app._calib", fake_calib_state)
    mocker.patch("physiclaw.core.server.app._phone", MagicMock())
    mocker.patch.object(warm_start, "_sanity", return_value=True)
    mocker.patch.object(warm_start.sys.stdin, "isatty", return_value=False)

    result = warm_start.try_resume(cam_index_override=None)

    assert result is True
    # Bundle replaced into the app + viewport_shift mirrored to bridge state.
    assert app.calibration is cal
    assert fake_calib_state.viewport_shift is cal.viewport_shift
    assert fake_calib_state.screen_dimension == (390, 844)
    app.assistive_touch.compute_at_screen_pos.assert_called_once_with(
        cal.viewport_shift,
    )
    app.connect_arm.assert_called_once()
    app.connect_camera.assert_called_once_with(1)
    # Origin re-pinned from the park spot so the bundle's affine stays valid.
    app.restore_park_origin.assert_called_once()
    app.home_screen.assert_called_once()
    app.mark_ready.assert_called_once()


@pytest.mark.integration
def test_try_resume_uses_cam_index_override(mocker) -> None:
    cal = _ready_bundle()
    app = _ready_app(cal)
    mocker.patch(
        "physiclaw.core.calibration.state.Calibration.load", return_value=cal,
    )
    mocker.patch("physiclaw.core.server.app.physiclaw", app)
    mocker.patch("physiclaw.core.server.app._calib", MagicMock())
    mocker.patch("physiclaw.core.server.app._phone", MagicMock())
    mocker.patch.object(warm_start, "_sanity", return_value=True)
    mocker.patch.object(warm_start.sys.stdin, "isatty", return_value=False)

    warm_start.try_resume(cam_index_override=3)

    app.connect_camera.assert_called_once_with(3)


@pytest.mark.integration
def test_try_resume_falls_back_to_cam_index_zero(mocker) -> None:
    cal = _ready_bundle()
    cal.cam_index = None
    app = _ready_app(cal)
    mocker.patch(
        "physiclaw.core.calibration.state.Calibration.load", return_value=cal,
    )
    mocker.patch("physiclaw.core.server.app.physiclaw", app)
    mocker.patch("physiclaw.core.server.app._calib", MagicMock())
    mocker.patch("physiclaw.core.server.app._phone", MagicMock())
    mocker.patch.object(warm_start, "_sanity", return_value=True)
    mocker.patch.object(warm_start.sys.stdin, "isatty", return_value=False)

    warm_start.try_resume(cam_index_override=None)

    app.connect_camera.assert_called_once_with(0)


@pytest.mark.integration
def test_try_resume_returns_false_when_bridge_never_connects(
    mocker, caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    cal = _ready_bundle()
    app = _ready_app(cal)
    app._bridge.wait_for_connection.return_value = False
    mocker.patch(
        "physiclaw.core.calibration.state.Calibration.load", return_value=cal,
    )
    mocker.patch("physiclaw.core.server.app.physiclaw", app)
    mocker.patch("physiclaw.core.server.app._calib", MagicMock())
    mocker.patch("physiclaw.core.server.app._phone", MagicMock())
    mocker.patch.object(warm_start.sys.stdin, "isatty", return_value=True)

    with caplog.at_level(logging.ERROR, logger="physiclaw.core.server.warm_start"):
        result = warm_start.try_resume(cam_index_override=None)

    assert result is False
    assert any("/bridge page not polling" in r.getMessage() for r in caplog.records)


@pytest.mark.integration
def test_try_resume_returns_false_when_sanity_fails(mocker) -> None:
    cal = _ready_bundle()
    app = _ready_app(cal)
    mocker.patch(
        "physiclaw.core.calibration.state.Calibration.load", return_value=cal,
    )
    mocker.patch("physiclaw.core.server.app.physiclaw", app)
    mocker.patch("physiclaw.core.server.app._calib", MagicMock())
    mocker.patch("physiclaw.core.server.app._phone", MagicMock())
    mocker.patch.object(warm_start, "_sanity", return_value=False)
    mocker.patch.object(warm_start.sys.stdin, "isatty", return_value=False)

    result = warm_start.try_resume(cam_index_override=None)

    assert result is False
    app.mark_ready.assert_not_called()


# ---------- _sanity ----------


@pytest.mark.integration
def test_sanity_passes_when_all_taps_within_tolerance(mocker) -> None:
    fake_validate = mocker.patch(
        "physiclaw.core.calibration.calibrate.validate_calibration",
        return_value=[
            {"passed": True, "error": 0.5},
            {"passed": True, "error": 0.6},
        ],
    )
    pl = MagicMock()
    pl.calibration = _ready_bundle()
    phone = MagicMock()

    out = warm_start._sanity(pl, MagicMock(), phone)

    assert out is True
    fake_validate.assert_called_once()
    # Bridge mode restored on success.
    assert phone.set_mode.call_args_list[-1].args == ("bridge",)


@pytest.mark.integration
def test_sanity_fails_when_no_taps_received(
    mocker, caplog: pytest.LogCaptureFixture,
) -> None:
    import logging
    mocker.patch(
        "physiclaw.core.calibration.calibrate.validate_calibration",
        return_value=[
            {"passed": False, "error": 999},
            {"passed": False, "error": 999},
        ],
    )
    pl = MagicMock()
    pl.calibration = _ready_bundle()

    with caplog.at_level(logging.ERROR, logger="physiclaw.core.server.warm_start"):
        out = warm_start._sanity(pl, MagicMock(), MagicMock())

    assert out is False
    assert any("no taps registered" in r.getMessage() for r in caplog.records)


@pytest.mark.integration
def test_sanity_fails_when_taps_received_but_off(
    mocker, caplog: pytest.LogCaptureFixture,
) -> None:
    import logging
    mocker.patch(
        "physiclaw.core.calibration.calibrate.validate_calibration",
        return_value=[
            {"passed": False, "error": 12.5},
            {"passed": True, "error": 1.0},
        ],
    )
    pl = MagicMock()
    pl.calibration = _ready_bundle()

    with caplog.at_level(logging.ERROR, logger="physiclaw.core.server.warm_start"):
        out = warm_start._sanity(pl, MagicMock(), MagicMock())

    assert out is False
    assert any("looks stale" in r.getMessage() for r in caplog.records)


@pytest.mark.integration
def test_sanity_restores_bridge_mode_on_validate_exception(mocker) -> None:
    mocker.patch(
        "physiclaw.core.calibration.calibrate.validate_calibration",
        side_effect=RuntimeError("hardware down"),
    )
    pl = MagicMock()
    pl.calibration = _ready_bundle()
    phone = MagicMock()

    with pytest.raises(RuntimeError):
        warm_start._sanity(pl, MagicMock(), phone)

    # Phone goes calibrate → bridge even on exception.
    assert phone.set_mode.call_args_list[-1].args == ("bridge",)
