"""Windows control/lifecycle composition with deterministic native-call barriers.

No device is opened. The fake reports DirectShow's raw Auto=1 / Manual=2
flags, retains device controls across reconnects, and rejects overlapping I/O.
"""

from collections import defaultdict, deque
from contextlib import contextmanager
from threading import Event, Lock, Thread, current_thread
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from physiclaw.core.hardware import camera as camera_mod
from physiclaw.core.hardware.camera import Camera
from physiclaw.core.hardware.device import DeviceNotFound
from physiclaw.port.backends import windows


class ObservedLock:
    def __init__(self):
        self.lock = Lock()
        self.waiting = defaultdict(Event)

    def acquire(self, *args, **kwargs):
        self.waiting[current_thread().name].set()
        return self.lock.acquire(*args, **kwargs)

    def release(self):
        self.lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_):
        self.release()


class WindowsDriver:
    def __init__(self):
        self.values = {
            cv2.CAP_PROP_AUTOFOCUS: 2,
            cv2.CAP_PROP_FOCUS: 60.0,
            cv2.CAP_PROP_AUTO_EXPOSURE: 2,
            cv2.CAP_PROP_EXPOSURE: -6,
            cv2.CAP_PROP_BACKEND: cv2.CAP_DSHOW,
        }
        self.frame = np.full((2, 1920, 3), 128, dtype=np.uint8)
        self.captures = []
        self.calls = []
        self.faults = defaultdict(deque)
        self.native_lock = Lock()
        self.block = None
        self.entered = Event()
        self.resume = Event()

    @contextmanager
    def native(self, capture, operation, key=None, value=None):
        assert self.native_lock.acquire(blocking=False), "overlapping native camera I/O"
        try:
            assert not capture.released, "control/read after capture release"
            self.calls.append((capture, operation, key, value))
            if (operation, key) == self.block:
                self.entered.set()
                assert self.resume.wait(3), "test did not release native-call barrier"
            yield
        finally:
            self.native_lock.release()

    def open(self, *_):
        capture = WindowsCapture(self)
        self.captures.append(capture)
        return capture


class WindowsCapture:
    def __init__(self, driver):
        self.driver = driver
        self.released = False

    def isOpened(self):
        return not self.released

    def read(self):
        with self.driver.native(self, "read"):
            return True, self.driver.frame

    def get(self, key):
        with self.driver.native(self, "get", key):
            if key == cv2.CAP_PROP_AUTO_EXPOSURE:
                # Some DirectShow drivers accept AE writes but cannot report
                # the mode. Requested state must not depend on this getter.
                return -1
            return self.driver.values.get(key, 0)

    def set(self, key, value):
        with self.driver.native(self, "set", key, value):
            faults = self.driver.faults[(key, value)]
            fault = faults.popleft() if faults else None
            if fault == "refuse":
                return False
            if fault == "raise":
                raise OSError("control write failed")
            if key in (cv2.CAP_PROP_AUTOFOCUS, cv2.CAP_PROP_AUTO_EXPOSURE):
                assert value in (0, 1), "DirectShow setters require boolean flags"
                self.driver.values[key] = 1 if value == 1 else 2
            else:
                self.driver.values[key] = value
            if fault == "partial_raise":
                raise OSError("control write failed after changing device")
            return True

    def release(self):
        if not self.released:
            with self.driver.native(self, "release"):
                self.released = True


@pytest.fixture
def windows_camera(monkeypatch):
    driver = WindowsDriver()
    gate = ObservedLock()
    monkeypatch.setattr(camera_mod.cv2, "VideoCapture", driver.open)
    monkeypatch.setattr(camera_mod.FrameReader, "start", lambda self: None)
    monkeypatch.setattr(camera_mod.CONFIG.camera, "auto_exposure", True)
    for name in (
        "camera_backend",
        "camera_size_cap",
        "camera_lock_focus",
        "camera_unlock_focus",
        "camera_read_focus",
        "camera_apply_focus",
        "camera_reset_controls",
        "camera_set_auto_exposure",
        "camera_set_manual_exposure",
    ):
        monkeypatch.setattr(camera_mod.platform, name, getattr(windows, name))
    monkeypatch.setattr(camera_mod.platform, "camera_capture_lock", lambda **_: gate)
    camera = Camera(index=0)
    try:
        yield SimpleNamespace(camera=camera, driver=driver, gate=gate)
    finally:
        driver.resume.set()
        camera.close()


def test_regression_windows_refused_unlock_is_retried_on_close(windows_camera):
    camera, driver = windows_camera.camera, windows_camera.driver
    assert camera.apply_focus(137)
    driver.faults[(cv2.CAP_PROP_AUTOFOCUS, 1)].append("refuse")

    camera.unlock_focus()
    needs_retry = camera._focus_needs_restore
    camera.close()

    assert needs_retry is True
    assert driver.values[cv2.CAP_PROP_AUTOFOCUS] == 1
    assert camera._focus_needs_restore is False
    assert driver.captures[-1].released


def test_windows_raw_manual_flag_is_not_misread_as_autofocus(windows_camera):
    camera, driver = windows_camera.camera, windows_camera.driver

    applied = camera.apply_focus(137)
    camera.set_manual_exposure(-5)
    camera._reopen()

    assert applied is True
    assert camera.focus_pinned is True
    assert camera.read_focus() == 137
    assert driver.values[cv2.CAP_PROP_AUTOFOCUS] == 2
    assert driver.values[cv2.CAP_PROP_AUTO_EXPOSURE] == 2
    assert driver.values[cv2.CAP_PROP_EXPOSURE] == -5
    assert len(driver.captures) == 2
    assert driver.captures[0].released


def test_refused_focus_and_rollback_keep_close_restoration_pending(windows_camera):
    camera, driver = windows_camera.camera, windows_camera.driver
    assert camera.apply_focus(137)
    driver.faults[(cv2.CAP_PROP_FOCUS, 200)].append("refuse")
    driver.faults[(cv2.CAP_PROP_AUTOFOCUS, 1)].append("refuse")

    applied = camera.apply_focus(200)
    needs_retry = camera._focus_needs_restore
    camera.close()

    assert applied is False
    assert needs_retry is True
    assert camera._focus_value is None
    assert camera.focus_pinned is False
    assert driver.values[cv2.CAP_PROP_AUTOFOCUS] == 1
    assert camera._focus_needs_restore is False


@pytest.mark.parametrize("fault", ["raise", "partial_raise"])
def test_thrown_focus_write_and_rollback_preserve_error_and_retry(
    windows_camera, fault
):
    camera, driver = windows_camera.camera, windows_camera.driver
    assert camera.apply_focus(137)
    driver.faults[(cv2.CAP_PROP_FOCUS, 200)].append(fault)
    driver.faults[(cv2.CAP_PROP_AUTOFOCUS, 1)].append("raise")

    with pytest.raises(OSError, match="control write failed"):
        camera.apply_focus(200)
    needs_retry = camera._focus_needs_restore
    camera.close()

    assert needs_retry is True
    assert camera._focus_value is None
    assert camera.focus_pinned is False
    assert driver.values[cv2.CAP_PROP_AUTOFOCUS] == 1
    assert driver.captures[-1].released


@pytest.mark.parametrize("fault", ["raise", "partial_raise"])
def test_exposure_partial_failure_replays_request_on_reopen(windows_camera, fault):
    camera, driver = windows_camera.camera, windows_camera.driver
    assert camera.apply_focus(137)
    driver.faults[(cv2.CAP_PROP_EXPOSURE, -5)].append(fault)

    with pytest.raises(OSError, match="control write failed"):
        camera.set_manual_exposure(-5)
    camera._reopen()

    assert driver.values[cv2.CAP_PROP_AUTO_EXPOSURE] == 2
    assert driver.values[cv2.CAP_PROP_EXPOSURE] == -5
    assert driver.values[cv2.CAP_PROP_AUTOFOCUS] == 2
    assert camera.focus_pinned is True


def test_refused_exposure_value_is_not_confused_with_physical_acceptance(
    windows_camera,
):
    camera, driver = windows_camera.camera, windows_camera.driver
    driver.faults[(cv2.CAP_PROP_EXPOSURE, -5)].append("refuse")

    camera.set_manual_exposure(-5)
    unchanged_value = driver.values[cv2.CAP_PROP_EXPOSURE]
    camera._reopen()

    assert unchanged_value == -6
    assert driver.values[cv2.CAP_PROP_EXPOSURE] == -5
    assert camera._exposure_value == -5


def _interleave(rig, first, queued):
    errors = []

    def invoke(operation):
        try:
            operation()
        except BaseException as error:
            errors.append(error)

    active = Thread(target=invoke, args=(first,), name="blocked-control", daemon=True)
    waiting = Thread(target=invoke, args=(queued,), name="queued-control", daemon=True)
    try:
        active.start()
        assert rig.driver.entered.wait(2)
        calls_before_queue = len(rig.driver.calls)
        waiting.start()
        assert rig.gate.waiting["queued-control"].wait(2)
        assert len(rig.driver.calls) == calls_before_queue
    finally:
        rig.driver.resume.set()
        active.join(timeout=2)
        if waiting.ident is not None:
            waiting.join(timeout=2)
    assert not active.is_alive() and not waiting.is_alive()
    assert errors == []


@pytest.mark.parametrize(
    ("method", "args", "exposure_flag", "focus_flag", "closed"),
    [
        ("set_manual_exposure", (-5,), 2, 2, False),
        ("set_auto_exposure", (), 1, 2, False),
        ("_reopen", (), 1, 2, False),
        ("close", (), 1, 1, True),
    ],
)
def test_partial_focus_transaction_excludes_exposure_reopen_and_close(
    windows_camera, method, args, exposure_flag, focus_flag, closed
):
    camera, driver = windows_camera.camera, windows_camera.driver
    driver.block = ("set", cv2.CAP_PROP_FOCUS)

    _interleave(
        windows_camera,
        lambda: camera.apply_focus(137),
        lambda: getattr(camera, method)(*args),
    )

    assert driver.values[cv2.CAP_PROP_AUTO_EXPOSURE] == exposure_flag
    assert driver.values[cv2.CAP_PROP_AUTOFOCUS] == focus_flag
    assert camera._closed is closed
    assert camera.focus_pinned is (not closed)


@pytest.mark.parametrize(
    ("method", "args", "focus_flag", "closed"),
    [
        ("apply_focus", (137,), 2, False),
        ("unlock_focus", (), 1, False),
        ("set_manual_exposure", (-5,), 1, False),
        ("_reopen", (), 1, False),
        ("close", (), 1, True),
    ],
)
def test_reader_native_call_excludes_controls_reopen_and_release(
    windows_camera, method, args, focus_flag, closed
):
    camera, driver = windows_camera.camera, windows_camera.driver
    driver.block = ("read", None)

    _interleave(
        windows_camera,
        camera._reader._read_frame,
        lambda: getattr(camera, method)(*args),
    )

    assert driver.values[cv2.CAP_PROP_AUTOFOCUS] == focus_flag
    assert camera._closed is closed
    assert driver.captures[-1].released is closed


@pytest.mark.parametrize(
    "method,args",
    [
        ("apply_focus", (137,)),
        ("set_manual_exposure", (-5,)),
        ("set_auto_exposure", ()),
        ("unlock_focus", ()),
    ],
)
def test_closed_windows_camera_cannot_mutate_replacement_controls(
    windows_camera, method, args
):
    camera, driver = windows_camera.camera, windows_camera.driver
    camera.close()
    calls = len(driver.calls)

    with pytest.raises(DeviceNotFound, match="closing or closed"):
        getattr(camera, method)(*args)
    camera._reopen()
    camera.close()

    assert len(driver.calls) == calls
