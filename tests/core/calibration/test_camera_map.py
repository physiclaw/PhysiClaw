"""Tests for `physiclaw.core.calibration.camera_map` — Mapping B.

`compute_camera_mapping` is driven with synthetic frames: red dots
rendered at the canonical grid positions (optionally pre-rotated, with
off-screen strays, or with a displaced dot) exercise the happy path,
the rotation application, the RGBM corner fence, and both fit-gate
failure modes — the real detection and fitting code runs throughout.
"""

from __future__ import annotations

from unittest.mock import MagicMock, PropertyMock

import cv2
import numpy as np
import pytest

from physiclaw.core.bridge.calib import CalibrationState
from physiclaw.core.calibration import camera_map as camera_map_mod
from physiclaw.core.calibration.transforms import ViewportShift
from tests.core.calibration.conftest import make_cal as _make_cal


def _grid_dot_image(rows=5, cols=3, w=600, h=900) -> np.ndarray:
    """Build a frame with red dots at grid_positions × frame size."""
    img = np.zeros((h, w, 3), dtype=np.uint8)
    for row in CalibrationState.GRID_ROWS_PCT:
        for col in CalibrationState.GRID_COLS_PCT:
            cv2.circle(
                img,
                (int(col * w), int(row * h)),
                radius=10,
                color=(0, 0, 255),
                thickness=-1,
            )
    return img


def test_compute_camera_mapping_succeeds(mocker) -> None:
    mocker.patch.object(camera_map_mod.time, "sleep")
    cam = MagicMock()
    cam.focus_lockable = False
    cam.raw_frame.return_value = _grid_dot_image()
    cal = _make_cal()

    pct_to_cam, cam_size, _ = camera_map_mod.compute_camera_mapping(
        cam, cal, rotation=-1
    )

    assert pct_to_cam.shape == (2, 3)
    assert cam_size == (600, 900)


def test_compute_camera_mapping_camera_dead(mocker) -> None:
    mocker.patch.object(camera_map_mod.time, "sleep")
    cam = MagicMock()
    cam.focus_lockable = False
    cam.raw_frame.return_value = None
    cal = _make_cal()

    with pytest.raises(RuntimeError, match="camera read failed"):
        camera_map_mod.compute_camera_mapping(cam, cal, rotation=-1)


def test_compute_camera_mapping_retries_then_fails(mocker) -> None:
    mocker.patch.object(camera_map_mod.time, "sleep")
    cam = MagicMock()
    cam.focus_lockable = False
    blank = np.zeros((600, 900, 3), dtype=np.uint8)
    cam.raw_frame.return_value = blank
    cal = _make_cal()

    with pytest.raises(RuntimeError, match=r"last saw 0 dots inside the screen"):
        camera_map_mod.compute_camera_mapping(cam, cal, rotation=-1)


def test_compute_camera_mapping_applies_rotation(mocker) -> None:
    mocker.patch.object(camera_map_mod.time, "sleep")
    cam = MagicMock()
    cam.focus_lockable = False
    # The camera returns a frame that is canonical only AFTER the configured
    # rotation is applied — so pre-rotate the canonical grid the opposite way.
    # This is the real scenario: rotation is picked to upright the grid, and
    # only then does sort_dots_to_grid (and the fit gate) line up.
    canonical = _grid_dot_image()
    cam.raw_frame.return_value = cv2.rotate(canonical, cv2.ROTATE_90_COUNTERCLOCKWISE)
    cal = _make_cal()

    rotate_spy = mocker.spy(camera_map_mod.cv2, "rotate")

    pct_to_cam, _, _ = camera_map_mod.compute_camera_mapping(
        cam, cal, rotation=cv2.ROTATE_90_CLOCKWISE
    )

    assert rotate_spy.called
    assert pct_to_cam.shape == (2, 3)


def _draw_corner_cluster(frame: np.ndarray, cx: int, cy: int, d: int = 20) -> None:
    """Draw a 2×2 RGBM corner cluster centered at (cx, cy)."""
    cv2.circle(frame, (cx - d, cy - d), 8, (0, 0, 255), -1)  # R
    cv2.circle(frame, (cx + d, cy - d), 8, (0, 255, 0), -1)  # G
    cv2.circle(frame, (cx + d, cy + d), 8, (255, 0, 0), -1)  # B
    cv2.circle(frame, (cx - d, cy + d), 8, (255, 0, 255), -1)  # M (magenta)


def test_compute_camera_mapping_masks_off_screen_reflection(mocker) -> None:
    # The corner frame bounds the screen; the grid frame carries the 15 dots
    # plus a stray red reflection outside that boundary. The mask drops the
    # reflection so the fit still sees a clean grid and succeeds.
    mocker.patch.object(camera_map_mod.time, "sleep")
    w, h = 600, 900
    corners_frame = np.zeros((h, w, 3), dtype=np.uint8)
    for cx, cy in [(60, 90), (540, 90), (540, 810), (60, 810)]:
        _draw_corner_cluster(corners_frame, cx, cy)
    grid_frame = _grid_dot_image()
    cv2.circle(grid_frame, (10, 450), 10, (0, 0, 255), -1)  # off-screen stray

    cam = MagicMock()
    cam.focus_lockable = False
    cam.raw_frame.side_effect = [corners_frame] + [grid_frame] * 5
    cal = _make_cal()

    pct_to_cam, cam_size, _ = camera_map_mod.compute_camera_mapping(
        cam, cal, rotation=-1
    )

    assert pct_to_cam.shape == (2, 3)
    assert cam_size == (600, 900)


def test_compute_camera_mapping_rejects_mis_corresponded_grid(mocker) -> None:
    # 15 dots are present but one is displaced into another row, so the fit can
    # never line up. The quality gate must reject it rather than return a
    # silently-wrong mapping.
    mocker.patch.object(camera_map_mod.time, "sleep")
    w, h = 600, 900
    frame = np.zeros((h, w, 3), dtype=np.uint8)
    positions = [
        (int(col * w), int(row * h))
        for row in CalibrationState.GRID_ROWS_PCT
        for col in CalibrationState.GRID_COLS_PCT
    ]
    positions[0] = (300, 850)  # displaced far from its grid slot
    for x, y in positions:
        cv2.circle(frame, (x, y), 10, (0, 0, 255), -1)
    cam = MagicMock()
    cam.focus_lockable = False
    cam.raw_frame.return_value = frame
    cal = _make_cal()

    with pytest.raises(RuntimeError, match=r"clean 15-dot grid passing the fit gate"):
        camera_map_mod.compute_camera_mapping(cam, cal, rotation=-1)


def test_compute_camera_mapping_uses_viewport_shift(mocker) -> None:
    mocker.patch.object(camera_map_mod.time, "sleep")
    cam = MagicMock()
    cam.focus_lockable = False
    cam.raw_frame.return_value = _grid_dot_image()
    vshift = ViewportShift(
        offset_x=0,
        offset_y=0,
        dpr=3.0,
        screenshot_width=1170,
        screenshot_height=2532,
    )
    cal = _make_cal(viewport_shift=vshift)
    cal.viewport_pct_to_screenshot_pct.side_effect = lambda c, r: (c, r)

    camera_map_mod.compute_camera_mapping(cam, cal, rotation=-1)

    assert cal.viewport_pct_to_screenshot_pct.call_count == 15


def test_compute_camera_mapping_requires_viewport_shift(mocker) -> None:
    """Same precondition as the arm fit — Mapping B must land in the
    canonical screenshot 0-1 space, never raw viewport 0-1."""
    mocker.patch.object(camera_map_mod.time, "sleep")
    cal = _make_cal(viewport_shift=None)

    with pytest.raises(RuntimeError, match="Viewport shift not measured"):
        camera_map_mod.compute_camera_mapping(MagicMock(), cal, rotation=-1)


# ---------- focus pin ----------


def _sharp_frame(w=600, h=900) -> np.ndarray:
    """High-variance noise — meters far above BLUR_THRESHOLD."""
    rng = np.random.default_rng(7)
    return rng.integers(0, 256, size=(h, w, 3), dtype=np.uint8)


def _lockable_cam(frame: np.ndarray | None) -> MagicMock:
    cam = MagicMock()
    cam.focus_lockable = True
    cam.wait_frames.return_value = True
    cam.raw_frame.return_value = frame
    cam.lock_focus.return_value = True
    return cam


def test_pin_focus_returns_none_when_not_lockable() -> None:
    cam = MagicMock()
    cam.focus_lockable = False

    assert camera_map_mod._pin_focus(cam, -1, None) is None
    cam.lock_focus.assert_not_called()
    cam.unlock_focus.assert_called_once()


def test_pin_focus_releases_a_stale_pin_before_relocking() -> None:
    # A re-run (mapping retried, or a setup re-run over a warm-started
    # session) arrives with the lens still pinned at the OLD position —
    # frozen, AF can't re-converge. The pin must start from live AF.
    cam = _lockable_cam(_sharp_frame())
    cam.read_focus.return_value = 123.0
    order: list[str] = []
    cam.unlock_focus.side_effect = lambda: order.append("unlock")
    cam.lock_focus.side_effect = lambda: order.append("lock") or True

    camera_map_mod._pin_focus(cam, -1, None)

    assert order[0] == "unlock"
    assert "lock" in order


def test_pin_focus_locks_reads_and_reapplies() -> None:
    # The verified position is read back AND re-applied through
    # apply_focus, so the camera remembers the exact value across a
    # reopen instead of re-freezing wherever the lens sits.
    cam = _lockable_cam(_sharp_frame())
    cam.read_focus.return_value = 123.0

    value = camera_map_mod._pin_focus(cam, -1, None)

    assert value == 123.0
    cam.apply_focus.assert_called_once_with(123.0)


def test_pin_focus_returns_none_when_scene_never_sharp(caplog) -> None:
    # A blank frame can't converge AF — the lock defers and nothing is
    # persisted (the bundle's cam_focus stays None; AF stays live).
    cam = _lockable_cam(np.zeros((900, 600, 3), dtype=np.uint8))

    value = camera_map_mod._pin_focus(cam, -1, None)

    assert value is None
    cam.read_focus.assert_not_called()
    assert any(
        record.levelname == "WARNING"
        and "could not be pinned" in getattr(record, "console_message", "")
        for record in caplog.records
    )


def test_pin_focus_returns_none_when_position_unreadable(caplog) -> None:
    # The freeze verified but the driver reports no position — nothing
    # a reconnect could replay, so the lens goes back to live AF and
    # every session behaves the same.
    cam = _lockable_cam(_sharp_frame())
    cam.read_focus.return_value = None

    value = camera_map_mod._pin_focus(cam, -1, None)

    assert value is None
    cam.apply_focus.assert_not_called()
    assert cam.unlock_focus.call_count == 2  # initial reset, then failed-pin rollback
    assert any(
        record.levelname == "WARNING"
        and "could not be saved" in getattr(record, "console_message", "")
        for record in caplog.records
    )


@pytest.mark.parametrize("focused_position", [0.0, 137.0])
@pytest.mark.parametrize(
    "absolute_write_accepted, rollback_accepted",
    [(True, True), (False, True), (False, False)],
)
def test_windows_focus_pin_preserves_and_replays_the_current_position(
    focused_position, absolute_write_accepted, rollback_accepted, caplog
) -> None:
    """Model the verified OpenCV MSMF/DirectShow control differences.

    MSMF reads the default and resets the lens on AF-off; DirectShow reads
    the current value and preserves it. Run the real Windows helpers and
    calibration pixel checks. A driver may reject DirectShow's flags=0
    absolute write even after accepting the explicit manual-mode toggle.
    """
    from physiclaw.port.backends import windows

    backend = windows.camera_backend()

    class Capture:
        position = focused_position
        autofocus = True

        def get(self, prop):
            assert prop == cv2.CAP_PROP_FOCUS
            return 42.0 if backend == cv2.CAP_MSMF else self.position

        def set(self, prop, value):
            if prop == cv2.CAP_PROP_AUTOFOCUS:
                if value and not self.autofocus and not rollback_accepted:
                    return False
                self.autofocus = bool(value)
                if self.autofocus:
                    self.position = focused_position
                elif backend == cv2.CAP_MSMF:
                    self.position = 0.0
            elif prop == cv2.CAP_PROP_FOCUS:
                if not absolute_write_accepted:
                    return False
                self.position = value
            else:
                raise AssertionError(prop)
            return True

    capture = Capture()
    sharp = _sharp_frame()
    cam = _lockable_cam(sharp)
    cam.raw_frame.side_effect = lambda: (
        sharp if capture.position == focused_position else np.zeros_like(sharp)
    )
    cam.lock_focus.side_effect = lambda: windows.camera_lock_focus(capture)
    cam.unlock_focus.side_effect = lambda: windows.camera_unlock_focus(capture)
    cam.read_focus.side_effect = lambda: windows.camera_read_focus(capture)
    cam.apply_focus.side_effect = lambda value: windows.camera_apply_focus(
        capture, value
    )

    value = camera_map_mod._pin_focus(cam, -1, None)

    assert value == (focused_position if absolute_write_accepted else None)
    assert capture.position == focused_position
    assert capture.autofocus is (not absolute_write_accepted and rollback_accepted)
    cam.apply_focus.assert_called_once_with(focused_position)
    assert cam.unlock_focus.call_count == (1 if absolute_write_accepted else 2)
    if not rollback_accepted:
        assert "could not re-enable autofocus" in caplog.text


def test_pin_focus_returns_none_when_apply_refused() -> None:
    # The driver froze the lens but rejects absolute writes — a
    # persisted value would fail to re-apply on every future connect,
    # so nothing is persisted and the lens goes back to AF.
    cam = _lockable_cam(_sharp_frame())
    cam.read_focus.return_value = 123.0
    cam.apply_focus.return_value = False

    value = camera_map_mod._pin_focus(cam, -1, None)

    assert value is None
    assert cam.unlock_focus.call_count == 2  # initial reset, then failed-pin rollback


def test_pin_focus_returns_none_when_applied_position_meters_soft() -> None:
    # The freeze verified sharp, but read-back + re-apply is a second
    # driver round-trip: an intermittent junk readback drives the lens
    # elsewhere. The applied state must be proven by pixels before the
    # value is persisted — on a soft read nothing is stored and the
    # lens goes back to AF.
    cam = _lockable_cam(_sharp_frame())
    cam.read_focus.return_value = 123.0

    def apply_and_blur(value: float) -> bool:
        cam.raw_frame.return_value = np.zeros((900, 600, 3), dtype=np.uint8)
        return True

    cam.apply_focus.side_effect = apply_and_blur

    value = camera_map_mod._pin_focus(cam, -1, None)

    assert value is None
    assert cam.unlock_focus.call_count == 2  # initial reset, then failed-pin rollback


def test_pin_focus_meters_only_the_screen_region() -> None:
    # Sharp content inside the corner-fenced screen, dark desk outside —
    # the meter must score the fence's bounding rect, not the full frame.
    frame = np.zeros((900, 600, 3), dtype=np.uint8)
    frame[100:800, 50:550] = _sharp_frame(w=500, h=700)
    poly = np.array([[50, 100], [550, 100], [550, 800], [50, 800]])

    region = camera_map_mod._focus_region(frame, poly)

    assert region.shape[:2] == (701, 501)  # boundingRect is inclusive


def test_focus_region_full_frame_without_fence() -> None:
    frame = np.zeros((900, 600, 3), dtype=np.uint8)

    assert camera_map_mod._focus_region(frame, None) is frame


def test_compute_camera_mapping_pins_during_the_focus_phase(mocker) -> None:
    # The pin must run while the dedicated checkerboard page is up, with
    # the grid phase following for dot detection (see hardware/focus.py).
    mocker.patch.object(camera_map_mod.time, "sleep")
    cam = MagicMock()
    cam.raw_frame.return_value = _grid_dot_image()
    cal = _make_cal()
    events: list[str] = []
    mocker.patch.object(cal, "set_phase", side_effect=lambda p, **kw: events.append(p))
    mocker.patch.object(
        camera_map_mod, "_pin_focus", side_effect=lambda *a, **kw: events.append("PIN")
    )

    camera_map_mod.compute_camera_mapping(cam, cal, rotation=-1)

    assert events[:4] == ["corners", "focus", "PIN", "grid"]


def test_compute_camera_mapping_returns_pinned_focus(mocker) -> None:
    mocker.patch.object(camera_map_mod.time, "sleep")
    cam = MagicMock()
    cam.focus_lockable = False
    cam.raw_frame.return_value = _grid_dot_image()
    cal = _make_cal()
    mocker.patch.object(camera_map_mod, "_pin_focus", return_value=42.0)

    _, _, cam_focus = camera_map_mod.compute_camera_mapping(cam, cal, rotation=-1)

    assert cam_focus == 42.0


class _PhaseCamera:
    """Device boundary fake: real images/focus metering, no native hardware."""

    focus_lockable = True

    @property
    def focus_pinned(self):
        return self.applied

    def __init__(self, cal, *, corners=None, grid=None):
        self.cal = cal
        self.frames = {
            "corners": np.zeros((900, 600, 3), np.uint8)
            if corners is None
            else corners,
            "focus": _sharp_frame(),
            "grid": _grid_dot_image() if grid is None else grid,
        }
        self.frozen = False
        self.applied = False
        self.unlocks = 0

    def raw_frame(self):
        return self.frames[self.cal.phase]

    def wait_frames(self, *args, **kwargs):
        return True

    def lock_focus(self):
        self.frozen = True
        return True

    def unlock_focus(self):
        self.frozen = self.applied = False
        self.unlocks += 1

    def read_focus(self):
        return 42.0

    def apply_focus(self, value):
        self.applied = True
        return True


def _real_cal():
    cal = CalibrationState()
    cal.viewport_shift = ViewportShift(0, 0, 1, 600, 900)
    cal.screen_dimension = {"viewport_width": 600, "viewport_height": 900}
    return cal


def _corner_frame(positions):
    frame = np.zeros((900, 600, 3), np.uint8)
    for x, y in positions:
        _draw_corner_cluster(frame, x, y)
    return frame


@pytest.mark.parametrize(
    "positions",
    [
        [(60, 90), (540, 90)],
        [(60, 90), (540, 300)],
        [(60, 90), (540, 90), (540, 810)],
    ],
)
def test_regression_partial_corners_do_not_clip_complete_grid(
    mocker, positions
) -> None:
    mocker.patch.object(camera_map_mod.time, "sleep")
    cal = _real_cal()
    cam = _PhaseCamera(cal, corners=_corner_frame(positions))

    affine, size, focus = camera_map_mod.compute_camera_mapping(cam, cal, -1)

    assert np.allclose(affine, np.eye(2, 3), atol=1e-6)
    assert size == (600, 900)
    assert focus == 42.0
    assert cam.frozen and cam.applied


def test_regression_expanded_focus_crop_clamps_negative_bounds(mocker) -> None:
    mocker.patch.object(camera_map_mod.time, "sleep")
    mocker.patch.object(
        camera_map_mod,
        "detect_screen_corners",
        return_value=[(5, 5), (595, 5), (595, 895), (5, 895)],
    )
    cal = _real_cal()
    cam = _PhaseCamera(cal)
    cam.frames["focus"][:, 300:] = 0

    _, _, focus = camera_map_mod.compute_camera_mapping(cam, cal, -1)

    assert focus == 42
    assert cam.frozen and cam.applied


@pytest.mark.parametrize(
    "positions",
    [
        [(60, 90), (540, 90), (540, 810), (400, 300)],
        [(60, 90), (200, 90), (400, 90), (540, 90)],
    ],
)
def test_camera_mapping_ignores_degenerate_four_corner_fence(mocker, positions) -> None:
    mocker.patch.object(camera_map_mod.time, "sleep")
    mocker.patch.object(camera_map_mod, "detect_screen_corners", return_value=positions)
    cal = _real_cal()
    cam = _PhaseCamera(cal)

    affine, size, focus = camera_map_mod.compute_camera_mapping(cam, cal, -1)

    assert np.allclose(affine, np.eye(2, 3), atol=1e-6)
    assert size == (600, 900)
    assert focus == 42


def test_focus_crop_outside_frame_fails_mapping_without_opencv_assertion(
    mocker,
) -> None:
    mocker.patch.object(camera_map_mod.time, "sleep")
    mocker.patch.object(
        camera_map_mod,
        "detect_screen_corners",
        return_value=[(-900, -900), (-600, -900), (-600, -600), (-900, -600)],
    )
    cal = _real_cal()
    cam = _PhaseCamera(cal)

    with pytest.raises(RuntimeError, match="clean 15-dot grid"):
        camera_map_mod.compute_camera_mapping(cam, cal, -1)

    assert cam.frozen is False
    assert cam.applied is False


def _fail_camera_stage(mocker, cam, stage, failure):
    read = cam.raw_frame

    def failed_frame():
        if (stage == "verification" and cam.applied) or (
            stage == "grid" and cam.cal.phase == "grid"
        ):
            raise failure
        return read()

    target = "raw_frame" if stage in ("verification", "grid") else stage
    effect = failed_frame if target == "raw_frame" else failure
    mocker.patch.object(cam, target, side_effect=effect)


@pytest.mark.parametrize("stage", ["read_focus", "apply_focus", "verification", "grid"])
@pytest.mark.parametrize(
    "failure", [OSError("device failed"), KeyboardInterrupt("cancelled")]
)
def test_regression_mapping_failures_restore_autofocus(mocker, stage, failure) -> None:
    mocker.patch.object(camera_map_mod.time, "sleep")
    cal = _real_cal()
    cam = _PhaseCamera(cal)
    _fail_camera_stage(mocker, cam, stage, failure)

    with pytest.raises(type(failure), match=str(failure)):
        camera_map_mod.compute_camera_mapping(cam, cal, -1)

    assert cam.frozen is False
    assert cam.applied is False
    assert cam.unlocks >= 2


def test_regression_exhausted_grid_fit_releases_successful_focus_pin(mocker) -> None:
    mocker.patch.object(camera_map_mod.time, "sleep")
    cal = _real_cal()
    cam = _PhaseCamera(cal, grid=np.zeros((900, 600, 3), np.uint8))

    with pytest.raises(RuntimeError, match="clean 15-dot grid"):
        camera_map_mod.compute_camera_mapping(cam, cal, -1)

    assert cam.frozen is False
    assert cam.applied is False


@pytest.mark.parametrize("stage", ["verification", "grid"])
def test_mapping_does_not_save_focus_lost_during_reconnect(mocker, stage):
    mocker.patch.object(camera_map_mod.time, "sleep")
    cal = _real_cal()
    cam = _PhaseCamera(cal)
    read = cam.raw_frame

    def reconnected_frame():
        # Model a reconnect whose focus restore failed. AF still produces
        # sharp pixels and a usable grid; neither proves a surviving pin.
        if (stage == "verification" and cam.applied) or (
            stage == "grid" and cal.phase == "grid"
        ):
            cam.frozen = cam.applied = False
        return read()

    mocker.patch.object(cam, "raw_frame", side_effect=reconnected_frame)

    affine, size, position = camera_map_mod.compute_camera_mapping(cam, cal, -1)

    assert affine is not None and size == (600, 900)
    assert position is None
    assert not cam.frozen and not cam.applied
    assert cam.unlocks == 2


def test_mapping_cleanup_failure_preserves_original_exception(mocker) -> None:
    mocker.patch.object(camera_map_mod.time, "sleep")
    cal = _real_cal()
    cam = _PhaseCamera(cal)
    mocker.patch.object(cam, "read_focus", side_effect=KeyboardInterrupt("original"))
    cleanup = mocker.patch.object(
        cam, "unlock_focus", side_effect=[None, OSError("cleanup")]
    )

    with pytest.raises(KeyboardInterrupt, match="original"):
        camera_map_mod.compute_camera_mapping(cam, cal, -1)

    assert cleanup.call_count == 2


def test_mapping_cleanup_does_not_depend_on_current_focus_capability(mocker):
    mocker.patch.object(camera_map_mod.time, "sleep")
    cal = _real_cal()
    cam = _PhaseCamera(cal)
    mocker.patch.object(cam, "read_focus", side_effect=KeyboardInterrupt("original"))
    capability = mocker.patch.object(
        _PhaseCamera,
        "focus_lockable",
        new_callable=PropertyMock,
        side_effect=[True, OSError("capability query failed")],
    )

    with pytest.raises(KeyboardInterrupt, match="original"):
        camera_map_mod.compute_camera_mapping(cam, cal, -1)

    capability.assert_called_once()
    assert not cam.frozen and not cam.applied
    assert cam.unlocks == 2


@pytest.mark.parametrize("stage", ["corners", "focus"])
def test_mapping_failure_before_focus_step_releases_previous_pin(mocker, stage):
    mocker.patch.object(camera_map_mod.time, "sleep")
    cal = _real_cal()
    cam = _PhaseCamera(cal)
    cam.frozen = cam.applied = True
    set_phase = cal.set_phase

    def fail_phase(phase):
        if phase == stage:
            raise OSError("phone disconnected")
        set_phase(phase)

    mocker.patch.object(cal, "set_phase", side_effect=fail_phase)

    with pytest.raises(OSError, match="phone disconnected"):
        camera_map_mod.compute_camera_mapping(cam, cal, -1)

    assert not cam.frozen and not cam.applied
    assert cam.unlocks == 1


@pytest.mark.parametrize(
    "affine",
    [
        np.full((2, 3), np.nan),
        np.zeros((2, 3)),
        np.array([[1e-320, 0, 0], [0, 1e-320, 0]]),
    ],
)
def test_camera_mapping_rejects_unusable_fit_and_releases_lens(mocker, affine) -> None:
    mocker.patch.object(camera_map_mod.time, "sleep")
    mocker.patch.object(
        camera_map_mod.cv2, "estimateAffine2D", return_value=(affine, np.ones((15, 1)))
    )
    cal = _real_cal()
    cam = _PhaseCamera(cal)

    with pytest.raises(RuntimeError, match="clean 15-dot grid"):
        camera_map_mod.compute_camera_mapping(cam, cal, -1)

    assert cam.frozen is False


def _compressed_grid():
    frame = np.zeros((1800, 600, 3), np.uint8)
    for row in CalibrationState.GRID_ROWS_PCT:
        for col in CalibrationState.GRID_COLS_PCT:
            center = (int(col * 600), int(900 + (row - 0.5) * 150))
            cv2.circle(frame, center, 5, (0, 0, 255), -1)
    return frame


def test_camera_mapping_rejects_singular_fit_despite_small_residual(mocker) -> None:
    mocker.patch.object(camera_map_mod.time, "sleep")
    # A nearly edge-on grid can have small residual even if the native fit
    # collapses one axis completely. Such a mapping cannot be inverted.
    mocker.patch.object(
        camera_map_mod.cv2,
        "estimateAffine2D",
        return_value=(np.array([[1.0, 0, 0], [0, 0, 0.5]]), np.ones((15, 1))),
    )
    cal = _real_cal()
    cam = _PhaseCamera(cal, grid=_compressed_grid())

    with pytest.raises(RuntimeError, match="clean 15-dot grid"):
        camera_map_mod.compute_camera_mapping(cam, cal, -1)

    assert cam.frozen is False
