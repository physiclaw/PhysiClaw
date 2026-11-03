"""Shared optical calibration through each platform's real control adapter."""

from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from physiclaw.core.calibration.camera_map import _pin_focus
from physiclaw.port.backends import darwin, dshow, linux, v4l2, windows


@pytest.mark.parametrize(
    "backend", [windows, darwin, linux], ids=["windows", "macos", "linux"]
)
@pytest.mark.parametrize("peak", [0, 32, 64])
@pytest.mark.parametrize(
    "working", [True, False], ids=["working-controls", "ignored-writes"]
)
def test_platform_calibration_selects_optical_peak_or_restores_af(
    mocker, backend, peak, working
):
    state = SimpleNamespace(autofocus=True, position=peak)
    bounds = (0, 64, 4)
    sharp = np.random.default_rng(1).integers(0, 256, (180, 120, 3), dtype=np.uint8)

    def set_focus(value):
        assert not state.autofocus
        if working:
            state.position = value
        return True

    def set_auto(value):
        state.autofocus = bool(value)
        if state.autofocus:
            state.position = peak
        return True

    class Capture:
        def set(self, prop, value):
            if prop == cv2.CAP_PROP_AUTOFOCUS:
                return set_auto(value)
            assert prop == cv2.CAP_PROP_FOCUS
            return set_focus(value)

        def get(self, prop):
            if prop == cv2.CAP_PROP_AUTOFOCUS:
                if backend is windows:
                    return 1 if state.autofocus else 2
                return int(state.autofocus)
            assert prop == cv2.CAP_PROP_FOCUS
            return 0 if backend is linux and state.autofocus else state.position

    def apply_uvc(value):
        set_auto(False)
        return set_focus(value)

    channel = SimpleNamespace(
        answers_focus=lambda: True,
        focus_range=lambda: bounds,
        apply_focus=apply_uvc,
        unlock_focus=lambda: set_auto(True),
    )
    mocker.patch.object(darwin, "_camera_terminal", return_value=channel)
    mocker.patch.object(dshow, "focus_range", return_value=bounds)
    mocker.patch.object(v4l2, "focus_range", return_value=bounds)
    capture = Capture()

    def frame():
        sigma = abs(state.position - peak) / 8
        return sharp if sigma == 0 else cv2.GaussianBlur(sharp, (0, 0), sigma)

    camera = mocker.Mock()
    camera.focus_lockable = backend.camera_focus_lockable()
    camera.focus_search_range = backend.camera_focus_search_range(0)
    camera.focus_pinned = True
    camera.wait_frames.return_value = True
    camera.raw_frame.side_effect = frame
    camera.apply_focus.side_effect = lambda value: backend.camera_apply_focus(
        capture, value
    )
    camera.unlock_focus.side_effect = lambda: backend.camera_unlock_focus(capture)

    result = _pin_focus(camera, -1, None)

    assert result == (peak if working else None)
    assert state.position == peak
    assert state.autofocus is not working
    camera.lock_focus.assert_not_called()
    camera.read_focus.assert_not_called()
