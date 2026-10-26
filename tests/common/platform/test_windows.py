"""Tests for `physiclaw.common.platform.windows` — Windows-specific helpers.

These run on every platform: subprocess, socket, and os.startfile are
mocked, so the windows module's dispatch logic is exercised regardless
of where the suite is run.
"""

from __future__ import annotations

import socket
from unittest.mock import MagicMock, call

import pytest

from physiclaw.common.platform import windows


@pytest.mark.parametrize("value", [float("inf"), float("nan"), -1.0])
def test_regression_read_nonfinite_focus_is_unavailable(value) -> None:
    cap = MagicMock()
    cap.get.return_value = value

    focus = windows.camera_read_focus(cap)

    assert focus is None


@pytest.mark.parametrize("value", [float("inf"), float("nan"), -1.0])
def test_regression_apply_invalid_focus_preserves_autofocus(value) -> None:
    cap = MagicMock()

    ok = windows.camera_apply_focus(cap, value)

    assert ok is False
    cap.set.assert_not_called()


# ---------- TRUST_PROXY_ENV ----------


def test_trust_proxy_env_is_false_on_windows() -> None:
    # Windows registry / env proxies don't reliably bypass loopback,
    # so urllib / httpx must skip system proxy on this platform.
    assert windows.TRUST_PROXY_ENV is False


# ---------- ensure_camera_permission ----------


def test_ensure_camera_permission_is_noop() -> None:
    # Access is managed through Windows privacy settings.
    assert windows.ensure_camera_permission() is None


# ---------- local_hostname ----------


def test_local_hostname_returns_short_name(mocker) -> None:
    mocker.patch.object(socket, "gethostname", return_value="DESKTOP-AB12.example")

    assert windows.local_hostname() == "DESKTOP-AB12"


def test_local_hostname_returns_none_on_empty(mocker) -> None:
    mocker.patch.object(socket, "gethostname", return_value="")

    assert windows.local_hostname() is None


def test_local_hostname_returns_none_on_failure(mocker) -> None:
    mocker.patch.object(socket, "gethostname", side_effect=OSError)

    assert windows.local_hostname() is None


# ---------- open_camera_aim_app / quit_camera_aim_app ----------


def test_open_camera_aim_app_runs_camera_uri(mocker) -> None:
    spy = mocker.patch.object(windows.subprocess, "run")

    windows.open_camera_aim_app()

    spy.assert_called_once()
    args = spy.call_args.args[0]
    assert args[0] == "cmd"
    assert "microsoft.windows.camera:" in args


def test_quit_camera_aim_app_runs_taskkill_and_settles(mocker) -> None:
    run_spy = mocker.patch.object(windows.subprocess, "run")
    sleep_spy = mocker.patch.object(windows.time, "sleep")

    windows.quit_camera_aim_app()

    run_spy.assert_called_once()
    args = run_spy.call_args.args[0]
    assert args[0] == "taskkill"
    assert "WindowsCamera.exe" in args
    sleep_spy.assert_called_once_with(0.5)


# ---------- open_image_files ----------


def test_open_image_files_calls_startfile_for_each(mocker) -> None:
    spy = mocker.patch.object(windows.os, "startfile", create=True)

    windows.open_image_files(["a.jpg", "b.jpg"])

    assert spy.call_args_list == [call("a.jpg"), call("b.jpg")]


def test_open_image_files_swallows_oserror_when_startfile_available(mocker) -> None:
    fake_startfile = MagicMock(side_effect=OSError)
    mocker.patch.object(windows.os, "startfile", fake_startfile, create=True)

    windows.open_image_files(["missing.jpg"])  # must not raise


def test_open_image_files_noop_on_empty_list(mocker) -> None:
    spy = MagicMock()
    mocker.patch.object(windows.os, "startfile", spy, create=True)

    windows.open_image_files([])

    spy.assert_not_called()


# ---------- camera exposure ----------


def test_camera_exposure_tunable_on_windows() -> None:
    assert windows.camera_exposure_tunable() is True


def test_no_camera_size_cap_on_windows() -> None:
    # Capture negotiation supplies the generic resolution fallbacks.
    assert windows.camera_size_cap() is None


def test_camera_backend_avoids_msmf_focus_readback_bug() -> None:
    # Keep capture consistent with the Windows camera-control implementation.
    import cv2

    assert windows.camera_backend() == cv2.CAP_DSHOW


def test_set_auto_exposure_dshow_encoding() -> None:
    # cap_dshow.cpp: round(value) == 1 enables auto exposure.
    import cv2

    cap = MagicMock()
    windows.camera_set_auto_exposure(cap)

    assert cap.set.call_args_list == [call(cv2.CAP_PROP_AUTO_EXPOSURE, 1)]


def test_set_manual_exposure_dshow_encoding() -> None:
    import cv2

    cap = MagicMock()
    windows.camera_set_manual_exposure(cap, -6)

    assert cap.set.call_args_list == [
        call(cv2.CAP_PROP_AUTO_EXPOSURE, 0),
        call(cv2.CAP_PROP_EXPOSURE, -6),
    ]


# ---------- camera focus ----------


def test_camera_focus_lockable_on_windows() -> None:
    # Always worth attempting — the lock call and measured sharpness
    # judge whether this camera honors it.
    assert windows.camera_focus_lockable() is True


@pytest.mark.parametrize("accepted", [True, False])
def test_lock_focus_uses_backend_current_position(accepted) -> None:
    import cv2

    cap = MagicMock()
    cap.set.return_value = accepted

    # DirectShow reads the current focus internally and refuses the toggle if
    # that read fails. Propagate its result without guessing another position.
    assert windows.camera_lock_focus(cap) is accepted
    cap.set.assert_called_once_with(cv2.CAP_PROP_AUTOFOCUS, 0)
    cap.get.assert_not_called()


@pytest.mark.parametrize("accepted", [True, False])
def test_unlock_focus_requests_af_and_reports_refusal(accepted, caplog) -> None:
    import cv2

    cap = MagicMock()
    cap.set.return_value = accepted
    windows.camera_unlock_focus(cap)

    assert cap.set.call_args_list == [call(cv2.CAP_PROP_AUTOFOCUS, 1)]
    assert ("could not re-enable autofocus" in caplog.text) is (not accepted)


@pytest.mark.parametrize("position", [0.0, 42.0])
def test_read_focus_returns_position(position) -> None:
    import cv2

    cap = MagicMock()
    cap.get.return_value = position

    assert windows.camera_read_focus(cap) == position
    cap.get.assert_called_once_with(cv2.CAP_PROP_FOCUS)


def test_read_focus_none_when_unreadable() -> None:
    cap = MagicMock()
    # cap_dshow.cpp returns -1 when the property read fails, not zero.
    cap.get.return_value = -1.0

    assert windows.camera_read_focus(cap) is None


@pytest.mark.parametrize("position", [0.0, 137.0])
@pytest.mark.parametrize("accepted", [True, False])
def test_apply_focus_disables_af_and_propagates_position_write_result(
    position, accepted
) -> None:
    import cv2

    cap = MagicMock()
    cap.set.side_effect = [True, accepted]

    assert windows.camera_apply_focus(cap, position) is accepted
    assert cap.set.call_args_list == [
        call(cv2.CAP_PROP_AUTOFOCUS, 0),
        call(cv2.CAP_PROP_FOCUS, position),
    ]


def test_apply_focus_fails_when_af_toggle_refused() -> None:
    cap = MagicMock()
    cap.set.return_value = False

    assert windows.camera_apply_focus(cap, 137.0) is False
    assert cap.set.call_count == 1  # never writes a position over live AF
