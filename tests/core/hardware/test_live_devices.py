"""Opt-in stationary checks for physical devices; never issue XY or coil-on commands.

Set PHYSICLAW_LIVE_SERIAL_PORT and PHYSICLAW_LIVE_CAMERA_INDEX, then run
pytest -m integration tests/core/hardware/test_live_devices.py.
Opening a serial connection can reset the board; close sends M5. Run with
the application server stopped so this suite owns both device handles.
"""

import math
import os
import time
from contextlib import contextmanager

import numpy as np
import pytest

from physiclaw.core.hardware.arm import StylusArm
from physiclaw.core.hardware.camera import Camera

pytestmark = pytest.mark.integration


@pytest.fixture
def live_arm():
    port = os.environ.get("PHYSICLAW_LIVE_SERIAL_PORT")
    if not port:
        pytest.skip("set PHYSICLAW_LIVE_SERIAL_PORT to opt into physical board access")
    arm = StylusArm(port=port)
    try:
        yield arm
    finally:
        arm.close()
        assert not arm.ser.is_open


@pytest.fixture
def camera_index():
    value = os.environ.get("PHYSICLAW_LIVE_CAMERA_INDEX")
    if value is None:
        pytest.skip(
            "set PHYSICLAW_LIVE_CAMERA_INDEX to opt into physical camera access"
        )
    return int(value)


@contextmanager
def _camera(index):
    camera = Camera(index=index)
    try:
        yield camera
    finally:
        camera.close()
        camera.close()
        assert not camera.health()
        assert not camera.cap.isOpened()


def _next_frame(camera, after):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        sample = camera.snapshot_with_time()
        if sample is not None and sample.published_at > after:
            return sample
        time.sleep(0.02)
    pytest.fail("camera did not publish a newer frame within five seconds")


def test_live_board_acknowledges_queries_and_reports_finite_position(live_arm):
    live_arm.transport.send("$I")

    status = live_arm.transport.query_status()
    position = live_arm.position()
    healthy = live_arm.health()

    assert status.startswith("<") and status.endswith(">")
    assert all(math.isfinite(coordinate) for coordinate in position)
    assert healthy
    assert live_arm.ser.timeout == 3
    assert live_arm.ser.write_timeout == 3


def test_live_camera_publishes_fresh_nonempty_frames(camera_index):
    with _camera(camera_index) as camera:
        first = _next_frame(camera, float("-inf"))

        second = _next_frame(camera, first.published_at)

        assert second.published_at > first.published_at
        assert second.frame.shape == first.frame.shape
        assert second.frame.ndim == 3 and second.frame.shape[2] == 3
        assert second.frame.dtype == np.uint8
        assert second.frame.size > 0 and np.any(second.frame)
        assert camera.health()


def test_live_camera_reopens_after_idempotent_close(camera_index):
    with _camera(camera_index) as first:
        original = _next_frame(first, float("-inf"))

    with _camera(camera_index) as reopened:
        current = _next_frame(reopened, float("-inf"))

        assert current.frame.shape == original.frame.shape
        assert current.published_at > original.published_at
        assert reopened.health()
