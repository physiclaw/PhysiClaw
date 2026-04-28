"""Tests for `physiclaw.core.server.warm_start` — focused on
`wait_for_port` (testable) and `try_resume` happy-path / early-return
branches with elaborate mocking. The `_sanity` helper is integration-
only and lives behind hardware fakes.
"""
from __future__ import annotations

import socket
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
    fake_cal.z_tap = -2.0
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
    fake_cal.z_tap = -2.0
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
