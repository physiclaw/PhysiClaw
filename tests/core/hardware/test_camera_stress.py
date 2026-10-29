"""Long focus histories through Camera and the real Linux control adapter.

The driver fake retains controls across release/open, as UVC hardware does.
This exercises composition of rollback, session memory, reconnect and close;
individual helper call ordering is covered by the ordinary camera tests.
"""

import random

import cv2
import numpy as np
import pytest

from physiclaw.core.hardware import camera as camera_mod
from physiclaw.core.hardware.camera import Camera
from physiclaw.core.hardware.device import DeviceNotFound
from physiclaw.port.backends import linux


@pytest.mark.parametrize("seed", [0, 42, 20261029])
def test_thousand_focus_transitions_with_driver_failures(monkeypatch, seed):
    rng = random.Random(seed)
    frame = np.full((8, 8, 3), 128, dtype=np.uint8)
    state = {cv2.CAP_PROP_AUTOFOCUS: 0.0, cv2.CAP_PROP_FOCUS: 600.0}
    captures = []
    writes = []
    failure = None

    class Capture:
        released = False

        def __init__(self, *args):
            captures.append(self)

        def isOpened(self):
            return not self.released

        def release(self):
            self.released = True

        def read(self):
            assert not self.released
            return True, frame

        def get(self, key):
            assert not self.released
            return state.get(key, 0.0)

        def set(self, key, value):
            nonlocal failure
            assert not self.released
            writes.append((key, value))
            if key == cv2.CAP_PROP_FOCUS:
                # Faults occur after the preceding AF-off write has succeeded.
                assert state[cv2.CAP_PROP_AUTOFOCUS] == 0
                fault, failure = failure, None
                if fault == "refuse":
                    return False
                if fault == "raise":
                    raise OSError("focus USB transfer failed")
            state[key] = float(value)
            return True

    monkeypatch.setattr(camera_mod.cv2, "VideoCapture", Capture)
    monkeypatch.setattr(camera_mod.FrameReader, "start", lambda self: None)
    for name in (
        "camera_backend",
        "camera_lock_focus",
        "camera_unlock_focus",
        "camera_read_focus",
        "camera_apply_focus",
        "camera_reset_controls",
        "camera_set_auto_exposure",
        "camera_set_manual_exposure",
    ):
        monkeypatch.setattr(camera_mod.platform, name, getattr(linux, name))
    # Expected session state is independent of the capture's driver properties.
    expected_pin = None
    cam = Camera(0)
    history = []
    try:
        for _ in range(1000):
            action = rng.choice(
                [
                    "apply",
                    "unlock",
                    "reject",
                    "raise",
                    "invalid",
                    "reopen",
                    "reopen_rejected",
                    "reopen_raises",
                    "freeze",
                ]
            )
            history.append(action)
            if action == "apply":
                expected_pin = float(rng.randrange(1024))
                assert cam.apply_focus(expected_pin)
            elif action in {"reject", "raise"}:
                failure = "refuse" if action == "reject" else "raise"
                expected_pin = None
                if action == "raise":
                    with pytest.raises(OSError, match="USB transfer"):
                        cam.apply_focus(555.0)
                else:
                    assert not cam.apply_focus(555.0)
            elif action == "invalid":
                assert not cam.apply_focus(rng.choice([-1, float("nan"), float("inf")]))
                expected_pin = None
            elif action == "unlock":
                cam.unlock_focus()
                expected_pin = None
            elif action == "freeze":
                # Calibration clears any old pin before an unremembered freeze.
                cam.unlock_focus()
                expected_pin = None
                assert cam.lock_focus()
                assert state[cv2.CAP_PROP_AUTOFOCUS] == 0
                assert not cam.focus_pinned
                cam.unlock_focus()
            else:
                if action != "reopen" and expected_pin is not None:
                    failure = "refuse" if action == "reopen_rejected" else "raise"
                    expected_pin = None
                cam._reopen()
                # A configure exception leaves a closed candidate; the next
                # reopen must select AF rather than replaying the failed pin.
                if captures[-1].released:
                    cam._reopen()
            assert failure is None, history[-10:]
            assert cam.focus_pinned == (expected_pin is not None), history[-10:]
            assert cam._focus_value == expected_pin, history[-10:]
            assert state[cv2.CAP_PROP_AUTOFOCUS] == int(expected_pin is None)
            if expected_pin is not None:
                assert state[cv2.CAP_PROP_FOCUS] == expected_pin
        assert cam.apply_focus(321)
    finally:
        cam.close()
    assert state[cv2.CAP_PROP_AUTOFOCUS] == 1
    assert all(cap.released for cap in captures)
    assert not cam.focus_pinned
    writes_at_close = len(writes)
    for method, args in [
        (cam.apply_focus, (321,)),
        (cam.unlock_focus, ()),
        (cam.lock_focus, ()),
        (cam.read_focus, ()),
    ]:
        with pytest.raises(DeviceNotFound):
            method(*args)
    cam._reopen()
    cam.close()
    assert len(writes) == writes_at_close
