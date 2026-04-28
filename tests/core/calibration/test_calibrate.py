"""Tests for `physiclaw.core.calibration.calibrate` — Phase 5 hardware fakes.

The big top-level orchestrators (`calibrate_arm`, `compute_camera_mapping`,
`validate_calibration`, `trace_screen_edge`, `verify_assistive_touch`)
are integration-tier loops with hundreds of LOC each — too tightly
coupled to live hardware to be tested unit-style. We cover the
testable helpers (`grid_positions`, `_find_viewport_cache`, `_tap_once`,
`_descend_to_contact`, `_pick_rotation_from_markers`, `_tap_and_read`,
`_tilt_from_affine`, `calibrate_camera_frame`, `measure_viewport_shift`).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import cv2
import numpy as np
import pytest

from physiclaw.core.calibration import calibrate as cal_mod
from physiclaw.core.calibration.calibrate import (
    PROBE_D,
    SLOW_Z_SPEED,
    TILT_ALIGNED_THRESHOLD,
    _descend_to_contact,
    _find_viewport_cache,
    _pick_rotation_from_markers,
    _tap_and_read,
    _tap_once,
    _tilt_from_affine,
    calibrate_camera_frame,
    grid_positions,
    measure_viewport_shift,
)
from physiclaw.core.bridge.calib import CalibrationState
from physiclaw.core.calibration.transforms import ViewportShift


pytestmark = [pytest.mark.integration]


# ---------- grid_positions ----------


def test_grid_positions_yields_15_in_outer_rows_inner_cols_order() -> None:
    cal = CalibrationState()

    out = list(grid_positions(cal))

    assert len(out) == 15
    # Outer iteration is rows; inner is cols.
    assert out[0] == (cal.GRID_COLS_PCT[0], cal.GRID_ROWS_PCT[0])
    assert out[1] == (cal.GRID_COLS_PCT[1], cal.GRID_ROWS_PCT[0])
    assert out[2] == (cal.GRID_COLS_PCT[2], cal.GRID_ROWS_PCT[0])
    assert out[3] == (cal.GRID_COLS_PCT[0], cal.GRID_ROWS_PCT[1])


# ---------- _find_viewport_cache ----------


def test_find_viewport_cache_returns_none_when_absent(
    tmp_path: Path, mocker,
) -> None:
    mocker.patch.object(cal_mod, "VIEWPORT_CACHE_STEM", tmp_path / "viewport")

    assert _find_viewport_cache() is None


def test_find_viewport_cache_returns_png_when_present(
    tmp_path: Path, mocker,
) -> None:
    stem = tmp_path / "viewport"
    png = stem.with_suffix(".png")
    png.write_bytes(b"\x89PNG")
    mocker.patch.object(cal_mod, "VIEWPORT_CACHE_STEM", stem)

    out = _find_viewport_cache()

    assert out == png


def test_find_viewport_cache_prefers_png_over_jpg(
    tmp_path: Path, mocker,
) -> None:
    stem = tmp_path / "viewport"
    stem.with_suffix(".png").write_bytes(b"png")
    stem.with_suffix(".jpg").write_bytes(b"jpg")
    mocker.patch.object(cal_mod, "VIEWPORT_CACHE_STEM", stem)

    assert _find_viewport_cache() == stem.with_suffix(".png")


def test_find_viewport_cache_falls_back_to_jpg(
    tmp_path: Path, mocker,
) -> None:
    stem = tmp_path / "viewport"
    jpg = stem.with_suffix(".jpg")
    jpg.write_bytes(b"jpg")
    mocker.patch.object(cal_mod, "VIEWPORT_CACHE_STEM", stem)

    assert _find_viewport_cache() == jpg


# ---------- _tap_once ----------


def test_tap_once_drives_pen_sequence() -> None:
    arm = MagicMock()

    _tap_once(arm, z=-2.5, z_speed=4000)

    arm._pen_down.assert_called_once_with(z=-2.5, speed=4000)
    arm._dwell.assert_called_once_with(0.15)
    arm._pen_up.assert_called_once()
    arm.wait_idle.assert_called_once()


# ---------- _descend_to_contact ----------


def test_descend_to_contact_returns_first_contact_z(mocker) -> None:
    mocker.patch.object(cal_mod.time, "sleep")
    arm = MagicMock()
    cal = MagicMock()
    # Two empty flushes (initial + first probe) then a contact event.
    cal.flush_touches.side_effect = [
        [], [], [{"x": 0.5, "y": 0.5}],
    ]

    z = _descend_to_contact(arm, cal, z_start=0.5, step=0.3)

    # First contact at z_start + step (after initial flush, first probe is z_start
    # and finds none, then z += step before second probe).
    assert z == 0.8


def test_descend_to_contact_raises_when_max_reached(mocker) -> None:
    mocker.patch.object(cal_mod.time, "sleep")
    arm = MagicMock()
    cal = MagicMock()
    cal.flush_touches.return_value = []  # never contacts

    with pytest.raises(RuntimeError, match="No touch detected"):
        _descend_to_contact(arm, cal, z_start=0.5, z_max=1.0, step=0.3)


# ---------- _pick_rotation_from_markers ----------


def test_pick_rotation_from_markers_no_rotation(mocker) -> None:
    """UP above RIGHT, mostly vertical separation → 0° / no rotation."""
    blob_calls = iter([
        (50.0, 100.0),  # blue UP at top
        (50.0, 200.0),  # red RIGHT at bottom
        None,            # no wrapped red
    ])
    mocker.patch(
        "physiclaw.core.vision.util.find_largest_hsv_blob",
        side_effect=lambda *a, **kw: next(blob_calls),
    )

    code, label = _pick_rotation_from_markers(np.zeros((400, 400, 3), np.uint8))

    assert code == -1
    assert "0°" in label


def test_pick_rotation_from_markers_90_clockwise(mocker) -> None:
    """UP at left, RIGHT at right, mostly horizontal → 90°."""
    blob_calls = iter([
        (100.0, 50.0),  # blue UP at left
        (200.0, 50.0),  # red RIGHT at right
        None,
    ])
    mocker.patch(
        "physiclaw.core.vision.util.find_largest_hsv_blob",
        side_effect=lambda *a, **kw: next(blob_calls),
    )

    code, label = _pick_rotation_from_markers(np.zeros((400, 400, 3), np.uint8))

    assert code == cv2.ROTATE_90_CLOCKWISE


def test_pick_rotation_from_markers_180(mocker) -> None:
    """UP below RIGHT, mostly vertical → 180°."""
    blob_calls = iter([
        (50.0, 200.0),  # blue UP at bottom
        (50.0, 100.0),  # red RIGHT at top
        None,
    ])
    mocker.patch(
        "physiclaw.core.vision.util.find_largest_hsv_blob",
        side_effect=lambda *a, **kw: next(blob_calls),
    )

    code, _ = _pick_rotation_from_markers(np.zeros((400, 400, 3), np.uint8))

    assert code == cv2.ROTATE_180


def test_pick_rotation_from_markers_90_counterclockwise(mocker) -> None:
    """Default fallthrough — UP right of RIGHT."""
    blob_calls = iter([
        (200.0, 50.0),  # blue UP at right
        (100.0, 50.0),  # red RIGHT at left
        None,
    ])
    mocker.patch(
        "physiclaw.core.vision.util.find_largest_hsv_blob",
        side_effect=lambda *a, **kw: next(blob_calls),
    )

    code, _ = _pick_rotation_from_markers(np.zeros((400, 400, 3), np.uint8))

    assert code == cv2.ROTATE_90_COUNTERCLOCKWISE


def test_pick_rotation_from_markers_uses_wrapped_red(mocker) -> None:
    """Second red search at the wrap range (170-180) is preferred when
    larger than the low-end red."""
    blob_calls = iter([
        (50.0, 100.0),    # blue UP
        (50.0, 200.0),    # low-end red (small)
        (60.0, 250.0),    # wrapped red — preferred
    ])
    mocker.patch(
        "physiclaw.core.vision.util.find_largest_hsv_blob",
        side_effect=lambda *a, **kw: next(blob_calls),
    )

    code, _ = _pick_rotation_from_markers(np.zeros((400, 400, 3), np.uint8))

    # red position now (60, 250) → still below blue (100) so 0° / no rotation.
    assert code == -1


def test_pick_rotation_from_markers_raises_when_marker_missing(mocker) -> None:
    """First _find_marker call returns None → RuntimeError."""
    mocker.patch(
        "physiclaw.core.vision.util.find_largest_hsv_blob",
        return_value=None,
    )

    with pytest.raises(RuntimeError, match="UP \\(blue\\) marker not found"):
        _pick_rotation_from_markers(np.zeros((400, 400, 3), np.uint8))


# ---------- _tap_and_read ----------


def test_tap_and_read_succeeds_first_attempt(mocker) -> None:
    mocker.patch.object(cal_mod.time, "sleep")
    arm = MagicMock()
    cal = MagicMock()
    cal.flush_touches.side_effect = [[], [{"x": 0.5, "y": 0.5}]]

    touch, z = _tap_and_read(arm, cal, gx=10, gy=10, z_tap=-2.0)

    assert touch == {"x": 0.5, "y": 0.5}
    assert z == -2.0  # not bumped


def test_tap_and_read_bumps_z_on_miss(mocker) -> None:
    mocker.patch.object(cal_mod.time, "sleep")
    arm = MagicMock()
    cal = MagicMock()
    cal.flush_touches.side_effect = [
        [], [],          # attempt 0: clear + miss
        [], [{"x": 1}],  # attempt 1: clear + hit at bumped z
    ]

    touch, z = _tap_and_read(arm, cal, gx=0, gy=0, z_tap=-2.0, max_retries=3)

    assert touch == {"x": 1}
    # z bumped once by 0.25.
    assert z == round(-2.0 + 0.25, 2)


def test_tap_and_read_returns_none_after_max_retries(mocker) -> None:
    mocker.patch.object(cal_mod.time, "sleep")
    arm = MagicMock()
    cal = MagicMock()
    cal.flush_touches.return_value = []  # always miss

    touch, z = _tap_and_read(arm, cal, gx=0, gy=0, z_tap=-2.0, max_retries=2)

    assert touch is None
    # Three attempts (initial + 2 retries) → z bumped twice.
    assert z == round(-2.0 + 0.5, 2)


# ---------- _tilt_from_affine ----------


def test_tilt_from_affine_zero_for_aligned_axes() -> None:
    # cv2.estimateAffine2D returns (2, 3): first two cols are the linear
    # part, last col the offset. `_tilt_from_affine` slices [:, :2].
    affine = np.array([
        [10.0, 0.0, 0.0],
        [0.0, 20.0, 0.0],
    ])

    tilt = _tilt_from_affine(affine)

    assert tilt == pytest.approx(0.0)


def test_tilt_from_affine_diagonal_returns_one() -> None:
    # 45° rotation — arm-X aligned diagonally with screen.
    affine = np.array([
        [1.0, 1.0, 0.0],
        [1.0, -1.0, 0.0],
    ])

    tilt = _tilt_from_affine(affine)

    assert tilt == pytest.approx(1.0)


def test_tilt_from_affine_singular_returns_one() -> None:
    # Singular linear part → LinAlgError → fallback 1.0.
    affine = np.array([
        [1.0, 1.0, 0.0],
        [2.0, 2.0, 0.0],
    ])

    tilt = _tilt_from_affine(affine)

    assert tilt == 1.0


# ---------- calibrate_camera_frame ----------


def test_calibrate_camera_frame_raises_when_camera_dead(mocker) -> None:
    mocker.patch.object(cal_mod.time, "sleep")
    cam = MagicMock()
    cam._fresh_frame.return_value = None
    cal = MagicMock()

    with pytest.raises(RuntimeError, match="Camera read failed"):
        calibrate_camera_frame(cam, cal)


def test_calibrate_camera_frame_returns_diagnostic_dict(mocker) -> None:
    mocker.patch.object(cal_mod.time, "sleep")
    cam = MagicMock()
    cam._fresh_frame.return_value = np.zeros((480, 640, 3), np.uint8)
    cal = MagicMock()
    mocker.patch.object(
        cal_mod, "check_phone_in_frame",
        return_value={
            "ok": True, "issues": [], "coverage": 0.9,
            "aspect_ratio": 16/9, "image_size": (480, 640),
            "phone_region": (0, 0, 100, 100),
        },
    )
    mocker.patch.object(
        cal_mod, "_pick_rotation_from_markers",
        return_value=(cv2.ROTATE_90_CLOCKWISE, "90° clockwise"),
    )

    out = calibrate_camera_frame(cam, cal)

    assert out["rotation"] == cv2.ROTATE_90_CLOCKWISE
    assert out["rotation_name"] == "90° clockwise"
    assert out["setup_ok"] is True
    assert out["coverage"] == 0.9
    cal.set_phase.assert_called_once_with("markers")


# ---------- measure_viewport_shift ----------


def _orange_square_image(
    *, css_size: int = 50, dpr: float = 3.0,
    expected_cx: int = 125, expected_cy: int = 225,
    actual_cx: int | None = None, actual_cy: int | None = None,
    sw: int = 1170, sh: int = 2532,
) -> bytes:
    """Build a JPEG with an orange square at a known position."""
    img = np.zeros((sh, sw, 3), dtype=np.uint8)
    px_size = int(css_size * dpr)
    cx = actual_cx if actual_cx is not None else int(expected_cx * dpr)
    cy = actual_cy if actual_cy is not None else int(expected_cy * dpr)
    half = px_size // 2
    # OpenCV BGR — orange is roughly (0, 165, 255).
    img[cy - half:cy + half, cx - half:cx + half] = (0, 165, 255)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return buf.tobytes()


def test_measure_viewport_shift_raises_when_dim_missing(mocker) -> None:
    mocker.patch.object(cal_mod.time, "sleep")
    cal = MagicMock()
    cal.screen_dimension = None

    with pytest.raises(RuntimeError, match="Screen dimension not received"):
        measure_viewport_shift(cal, MagicMock())


def test_measure_viewport_shift_raises_when_dim_zero_width(mocker) -> None:
    mocker.patch.object(cal_mod.time, "sleep")
    cal = MagicMock()
    cal.screen_dimension = {"viewport_width": 0, "viewport_height": 100}

    with pytest.raises(RuntimeError, match="Screen dimension not received"):
        measure_viewport_shift(cal, MagicMock())


def test_measure_viewport_shift_raises_when_screenshot_timeout(
    mocker, tmp_path: Path,
) -> None:
    mocker.patch.object(cal_mod.time, "sleep")
    mocker.patch.object(
        cal_mod, "VIEWPORT_CACHE_STEM", tmp_path / "viewport",
    )
    cal = MagicMock()
    cal.screen_dimension = {"viewport_width": 390, "viewport_height": 844}
    bridge = MagicMock()
    bridge.wait_screenshot.return_value = None

    with pytest.raises(RuntimeError, match="Timeout"):
        measure_viewport_shift(cal, bridge, fresh=True)


def test_measure_viewport_shift_raises_on_decode_failure(
    mocker, tmp_path: Path,
) -> None:
    mocker.patch.object(cal_mod.time, "sleep")
    mocker.patch.object(
        cal_mod, "VIEWPORT_CACHE_STEM", tmp_path / "viewport",
    )
    cal = MagicMock()
    cal.screen_dimension = {"viewport_width": 390, "viewport_height": 844}
    bridge = MagicMock()
    bridge.wait_screenshot.return_value = b"not an image"

    with pytest.raises(RuntimeError, match="Failed to decode"):
        measure_viewport_shift(cal, bridge, fresh=True)


def test_measure_viewport_shift_raises_when_no_orange_detected(
    mocker, tmp_path: Path,
) -> None:
    mocker.patch.object(cal_mod.time, "sleep")
    mocker.patch.object(
        cal_mod, "VIEWPORT_CACHE_STEM", tmp_path / "viewport",
    )
    cal = MagicMock()
    cal.screen_dimension = {"viewport_width": 390, "viewport_height": 844}
    # All-black image — no orange to find.
    img = np.zeros((400, 400, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", img)
    bridge = MagicMock()
    bridge.wait_screenshot.return_value = buf.tobytes()

    with pytest.raises(RuntimeError, match="Could not detect orange square"):
        measure_viewport_shift(cal, bridge, fresh=True)


def test_measure_viewport_shift_succeeds_and_caches(
    mocker, tmp_path: Path,
) -> None:
    mocker.patch.object(cal_mod.time, "sleep")
    cache_stem = tmp_path / "viewport"
    mocker.patch.object(cal_mod, "VIEWPORT_CACHE_STEM", cache_stem)
    cal = MagicMock()
    cal.screen_dimension = {"viewport_width": 390, "viewport_height": 844}
    bridge = MagicMock()
    bridge.wait_screenshot.return_value = _orange_square_image()

    transform = measure_viewport_shift(cal, bridge, fresh=True)

    assert isinstance(transform, ViewportShift)
    assert transform.dpr > 0
    assert cal.viewport_shift is transform
    # Cache was written.
    assert cache_stem.with_suffix(".jpg").exists()


def test_measure_viewport_shift_uses_cache_when_not_fresh(
    mocker, tmp_path: Path,
) -> None:
    mocker.patch.object(cal_mod.time, "sleep")
    cache_stem = tmp_path / "viewport"
    cache_stem.with_suffix(".png").write_bytes(_orange_square_image())
    mocker.patch.object(cal_mod, "VIEWPORT_CACHE_STEM", cache_stem)
    cal = MagicMock()
    cal.screen_dimension = {"viewport_width": 390, "viewport_height": 844}
    bridge = MagicMock()

    transform = measure_viewport_shift(cal, bridge, fresh=False)

    assert isinstance(transform, ViewportShift)
    # Cache hit path → bridge.wait_screenshot never called.
    bridge.wait_screenshot.assert_not_called()


def test_measure_viewport_shift_png_cache_extension(
    mocker, tmp_path: Path,
) -> None:
    mocker.patch.object(cal_mod.time, "sleep")
    cache_stem = tmp_path / "viewport"
    mocker.patch.object(cal_mod, "VIEWPORT_CACHE_STEM", cache_stem)
    cal = MagicMock()
    cal.screen_dimension = {"viewport_width": 390, "viewport_height": 844}
    bridge = MagicMock()
    # Build a real PNG with the orange square.
    img = np.zeros((400, 400, 3), dtype=np.uint8)
    cv2.rectangle(img, (75, 175), (175, 275), (0, 165, 255), -1)
    ok, buf = cv2.imencode(".png", img)
    bridge.wait_screenshot.return_value = buf.tobytes()

    measure_viewport_shift(cal, bridge, fresh=True)

    # Cached as .png because PNG signature detected.
    assert cache_stem.with_suffix(".png").exists()


# ---------- calibrate_arm ----------


def _make_cal(*, viewport_shift=None) -> MagicMock:
    """A CalibrationState mock with the grid constants surfaced."""
    cal = MagicMock()
    cal.GRID_COLS_PCT = CalibrationState.GRID_COLS_PCT
    cal.GRID_ROWS_PCT = CalibrationState.GRID_ROWS_PCT
    cal.viewport_shift = viewport_shift
    return cal


def _scripted_touches(touches: list[dict]) -> "callable":
    """Build a flush_touches side_effect that yields one touch per call,
    interleaved with empty pre-tap clears."""
    queue = list(touches)

    def _flush():
        # Two flushes per tap: clear (returns []), then read (returns one).
        # We cycle: every odd-call-index returns the next scripted touch.
        if not _flush.call_count % 2:
            _flush.call_count += 1
            return []
        if queue:
            _flush.call_count += 1
            return [queue.pop(0)]
        _flush.call_count += 1
        return []

    _flush.call_count = 0
    return _flush


def test_calibrate_arm_succeeds_with_z_hint(mocker) -> None:
    mocker.patch.object(cal_mod.time, "sleep")
    arm = MagicMock()
    cal = _make_cal()

    # 3 probes + 15 grid taps = 18 touches. Use a regular grid for clean
    # affine fit. Probes at screen (0.5, 0.5), (0.6, 0.5), (0.5, 0.6),
    # then 15 grid points scaled the same way.
    probe_touches = [
        {"x": 0.5, "y": 0.5},
        {"x": 0.6, "y": 0.5},
        {"x": 0.5, "y": 0.6},
    ]
    grid_touches = [
        {"x": col, "y": row}
        for row in cal.GRID_ROWS_PCT
        for col in cal.GRID_COLS_PCT
    ]

    # Mock _tap_and_read directly to bypass the inner flush_touches loop.
    tap_returns = iter([
        (probe_touches[0], -2.0),
        (probe_touches[1], -2.0),
        (probe_touches[2], -2.0),
    ] + [(t, -2.0) for t in grid_touches])
    mocker.patch.object(cal_mod, "_tap_and_read", side_effect=lambda *a, **kw: next(tap_returns))

    z_tap, pct_to_grbl, tilt, gtouches = cal_mod.calibrate_arm(
        arm, cal, z_tap_hint=-2.0,
    )

    assert z_tap == -2.0
    assert pct_to_grbl.shape == (2, 3)
    assert isinstance(tilt, float)
    assert len(gtouches) == 15
    arm.set_origin.assert_called_once()


def test_calibrate_arm_descends_when_no_hint(mocker) -> None:
    mocker.patch.object(cal_mod.time, "sleep")
    arm = MagicMock()
    cal = _make_cal()

    descend_spy = mocker.patch.object(
        cal_mod, "_descend_to_contact", return_value=-2.0,
    )
    probe = [
        {"x": 0.5, "y": 0.5},
        {"x": 0.6, "y": 0.5},
        {"x": 0.5, "y": 0.6},
    ]
    grid = [
        {"x": col, "y": row}
        for row in cal.GRID_ROWS_PCT
        for col in cal.GRID_COLS_PCT
    ]
    tap_returns = iter([
        (probe[0], -1.75), (probe[1], -1.75), (probe[2], -1.75),
    ] + [(t, -1.75) for t in grid])
    mocker.patch.object(cal_mod, "_tap_and_read", side_effect=lambda *a, **kw: next(tap_returns))

    cal_mod.calibrate_arm(arm, cal)

    descend_spy.assert_called_once()


def test_calibrate_arm_raises_on_probe_center_miss(mocker) -> None:
    mocker.patch.object(cal_mod.time, "sleep")
    arm = MagicMock()
    cal = _make_cal()
    mocker.patch.object(
        cal_mod, "_tap_and_read", side_effect=[(None, -2.0)],
    )

    with pytest.raises(RuntimeError, match="no touch at center"):
        cal_mod.calibrate_arm(arm, cal, z_tap_hint=-2.0)


def test_calibrate_arm_raises_on_probe_x_miss(mocker) -> None:
    mocker.patch.object(cal_mod.time, "sleep")
    arm = MagicMock()
    cal = _make_cal()
    mocker.patch.object(
        cal_mod, "_tap_and_read",
        side_effect=[
            ({"x": 0.5, "y": 0.5}, -2.0),
            (None, -2.0),
        ],
    )

    with pytest.raises(RuntimeError, match="no touch at \\+10mm X"):
        cal_mod.calibrate_arm(arm, cal, z_tap_hint=-2.0)


def test_calibrate_arm_raises_on_probe_y_miss(mocker) -> None:
    mocker.patch.object(cal_mod.time, "sleep")
    arm = MagicMock()
    cal = _make_cal()
    mocker.patch.object(
        cal_mod, "_tap_and_read",
        side_effect=[
            ({"x": 0.5, "y": 0.5}, -2.0),
            ({"x": 0.6, "y": 0.5}, -2.0),
            (None, -2.0),
        ],
    )

    with pytest.raises(RuntimeError, match="no touch at \\+10mm Y"):
        cal_mod.calibrate_arm(arm, cal, z_tap_hint=-2.0)


def test_calibrate_arm_raises_when_too_few_grid_hits(mocker) -> None:
    mocker.patch.object(cal_mod.time, "sleep")
    arm = MagicMock()
    cal = _make_cal()
    # 3 probes succeed, all 15 grid taps miss (None) — only 3 valid pairs.
    probe = [
        ({"x": 0.5, "y": 0.5}, -2.0),
        ({"x": 0.6, "y": 0.5}, -2.0),
        ({"x": 0.5, "y": 0.6}, -2.0),
    ]
    grid_misses = [(None, -2.0)] * 15
    mocker.patch.object(
        cal_mod, "_tap_and_read", side_effect=probe + grid_misses,
    )

    with pytest.raises(RuntimeError, match="only 3 valid taps"):
        cal_mod.calibrate_arm(arm, cal, z_tap_hint=-2.0)


def test_calibrate_arm_uses_viewport_pct_when_shift_set(mocker) -> None:
    mocker.patch.object(cal_mod.time, "sleep")
    arm = MagicMock()
    vshift = ViewportShift(
        offset_x=0, offset_y=0, dpr=3.0,
        screenshot_width=1170, screenshot_height=2532,
    )
    cal = _make_cal(viewport_shift=vshift)
    cal.viewport_pct_to_screenshot_pct.side_effect = lambda c, r: (c, r)

    probe = [
        ({"x": 0.5, "y": 0.5}, -2.0),
        ({"x": 0.6, "y": 0.5}, -2.0),
        ({"x": 0.5, "y": 0.6}, -2.0),
    ]
    grid = [
        ({"x": col, "y": row}, -2.0)
        for row in cal.GRID_ROWS_PCT
        for col in cal.GRID_COLS_PCT
    ]
    mocker.patch.object(cal_mod, "_tap_and_read", side_effect=probe + grid)

    cal_mod.calibrate_arm(arm, cal, z_tap_hint=-2.0)

    # viewport_pct_to_screenshot_pct was used for grid prediction.
    assert cal.viewport_pct_to_screenshot_pct.call_count == 15


# ---------- compute_camera_mapping ----------


def _grid_dot_image(rows=5, cols=3, w=600, h=900) -> np.ndarray:
    """Build a frame with red dots at grid_positions × frame size."""
    img = np.zeros((h, w, 3), dtype=np.uint8)
    for row in CalibrationState.GRID_ROWS_PCT:
        for col in CalibrationState.GRID_COLS_PCT:
            cv2.circle(
                img, (int(col * w), int(row * h)),
                radius=10, color=(0, 0, 255), thickness=-1,
            )
    return img


def test_compute_camera_mapping_succeeds(mocker) -> None:
    mocker.patch.object(cal_mod.time, "sleep")
    cam = MagicMock()
    cam._fresh_frame.return_value = _grid_dot_image()
    cal = _make_cal()

    pct_to_cam, cam_size = cal_mod.compute_camera_mapping(cam, cal, rotation=-1)

    assert pct_to_cam.shape == (2, 3)
    assert cam_size == (600, 900)


def test_compute_camera_mapping_camera_dead(mocker) -> None:
    mocker.patch.object(cal_mod.time, "sleep")
    cam = MagicMock()
    cam._fresh_frame.return_value = None
    cal = _make_cal()

    with pytest.raises(RuntimeError, match="camera read failed"):
        cal_mod.compute_camera_mapping(cam, cal, rotation=-1)


def test_compute_camera_mapping_retries_then_fails(mocker) -> None:
    mocker.patch.object(cal_mod.time, "sleep")
    cam = MagicMock()
    blank = np.zeros((600, 900, 3), dtype=np.uint8)
    cam._fresh_frame.return_value = blank
    cal = _make_cal()

    with pytest.raises(RuntimeError, match=r"detected 0 dots, expected 15"):
        cal_mod.compute_camera_mapping(cam, cal, rotation=-1)


def test_compute_camera_mapping_applies_rotation(mocker) -> None:
    mocker.patch.object(cal_mod.time, "sleep")
    cam = MagicMock()
    # Pre-rotated dot image; camera returns the source orientation.
    img = _grid_dot_image()
    cam._fresh_frame.return_value = img
    cal = _make_cal()

    rotate_spy = mocker.spy(cal_mod.cv2, "rotate")

    cal_mod.compute_camera_mapping(cam, cal, rotation=cv2.ROTATE_90_CLOCKWISE)

    assert rotate_spy.called


def test_compute_camera_mapping_uses_viewport_shift(mocker) -> None:
    mocker.patch.object(cal_mod.time, "sleep")
    cam = MagicMock()
    cam._fresh_frame.return_value = _grid_dot_image()
    vshift = ViewportShift(
        offset_x=0, offset_y=0, dpr=3.0,
        screenshot_width=1170, screenshot_height=2532,
    )
    cal = _make_cal(viewport_shift=vshift)
    cal.viewport_pct_to_screenshot_pct.side_effect = lambda c, r: (c, r)

    cal_mod.compute_camera_mapping(cam, cal, rotation=-1)

    assert cal.viewport_pct_to_screenshot_pct.call_count == 15


# ---------- validate_calibration ----------


def _identity_pct_to_grbl() -> np.ndarray:
    return np.array([
        [10.0, 0.0, 0.0],
        [0.0, 20.0, 0.0],
    ])


def _identity_pct_to_cam() -> np.ndarray:
    return np.array([
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
    ])


def test_validate_calibration_records_passed_when_touches_match(mocker) -> None:
    mocker.patch.object(cal_mod.time, "sleep")
    mocker.patch.object(cal_mod.random, "random", return_value=0.5)
    arm = MagicMock()
    cam = MagicMock()
    cam._fresh_frame.return_value = np.zeros((100, 100, 3), np.uint8)
    cal = _make_cal()

    # detect_orange_dot returns the dot at the same pct (camera 0-1 = screen 0-1).
    mocker.patch.object(
        cal_mod, "_detect_orange_dot",
        side_effect=lambda f: (50, 50),  # at center of 100×100 → 0.5 pct
    )
    # Touches always come back at the expected position.
    cal.flush_touches.side_effect = (
        [[], [{"x": 0.5, "y": 0.5}]] * 5
    )

    results = cal_mod.validate_calibration(
        arm, cam, cal, z_tap=-2.0, rotation=-1,
        pct_to_grbl=_identity_pct_to_grbl(),
        pct_to_cam=_identity_pct_to_cam(),
        cam_size=(100, 100), num_tests=2,
    )

    assert len(results) == 2
    assert all(r["passed"] for r in results)


def test_validate_calibration_falls_back_when_dot_undetected(mocker) -> None:
    mocker.patch.object(cal_mod.time, "sleep")
    mocker.patch.object(cal_mod.random, "random", return_value=0.5)
    arm = MagicMock()
    cam = MagicMock()
    cam._fresh_frame.return_value = np.zeros((100, 100, 3), np.uint8)
    cal = _make_cal()
    mocker.patch.object(cal_mod, "_detect_orange_dot", return_value=None)
    cal.flush_touches.side_effect = (
        [[], [{"x": 0.5, "y": 0.5}]] * 1
    )

    results = cal_mod.validate_calibration(
        arm, cam, cal, z_tap=-2.0, rotation=-1,
        pct_to_grbl=_identity_pct_to_grbl(),
        pct_to_cam=_identity_pct_to_cam(),
        cam_size=(100, 100), num_tests=1,
    )

    assert len(results) == 1
    # Fallback used expected position → should pass.
    assert results[0]["passed"] is True


def test_validate_calibration_records_failure_on_no_touch(mocker) -> None:
    mocker.patch.object(cal_mod.time, "sleep")
    mocker.patch.object(cal_mod.random, "random", return_value=0.5)
    arm = MagicMock()
    cam = MagicMock()
    cam._fresh_frame.return_value = np.zeros((100, 100, 3), np.uint8)
    cal = _make_cal()
    mocker.patch.object(
        cal_mod, "_detect_orange_dot", return_value=(50, 50),
    )
    # All flushes return empty → tap retries 4 times, each fails.
    cal.flush_touches.return_value = []

    results = cal_mod.validate_calibration(
        arm, cam, cal, z_tap=-2.0, rotation=-1,
        pct_to_grbl=_identity_pct_to_grbl(),
        pct_to_cam=_identity_pct_to_cam(),
        cam_size=(100, 100), num_tests=1,
    )

    assert len(results) == 1
    assert results[0]["passed"] is False
    assert results[0]["error"] == 999.0


def test_validate_calibration_retries_with_z_bump_on_miss(mocker) -> None:
    mocker.patch.object(cal_mod.time, "sleep")
    mocker.patch.object(cal_mod.random, "random", return_value=0.5)
    arm = MagicMock()
    cam = MagicMock()
    cam._fresh_frame.return_value = np.zeros((100, 100, 3), np.uint8)
    cal = _make_cal()
    mocker.patch.object(cal_mod, "_detect_orange_dot", return_value=(50, 50))

    # First two flushes (clear + miss) then clear + hit on retry.
    cal.flush_touches.side_effect = [
        [], [],                      # attempt 0: clear + miss
        [], [{"x": 0.5, "y": 0.5}],  # attempt 1: clear + hit
    ]

    results = cal_mod.validate_calibration(
        arm, cam, cal, z_tap=-2.0, rotation=-1,
        pct_to_grbl=_identity_pct_to_grbl(),
        pct_to_cam=_identity_pct_to_cam(),
        cam_size=(100, 100), num_tests=1,
    )

    assert results[0]["passed"] is True


# ---------- trace_screen_edge ----------


def test_trace_screen_edge_visits_all_check_points(mocker) -> None:
    mocker.patch.object(cal_mod.time, "sleep")
    arm = MagicMock()
    transforms = MagicMock()
    transforms.pct_to_grbl_mm.side_effect = lambda x, y: (x * 100, y * 100)

    cal_mod.trace_screen_edge(arm, transforms)

    # 9 check points + 2 return_to_origin → 9 fast moves + 2 origin returns.
    assert arm._fast_move.call_count == 9
    assert arm.return_to_origin.call_count == 2


# ---------- verify_assistive_touch ----------


def test_verify_assistive_touch_raises_when_at_unset(mocker) -> None:
    arm = MagicMock()
    at = MagicMock()
    at.at_screen = None

    with pytest.raises(RuntimeError, match="AT position not set"):
        cal_mod.verify_assistive_touch(arm, at, MagicMock(), MagicMock(), _identity_pct_to_grbl())


def test_verify_assistive_touch_raises_when_no_nonce(mocker) -> None:
    arm = MagicMock()
    at = MagicMock()
    at.at_screen = (0.05, 0.1)
    cal = _make_cal()
    cal._screenshot_nonce = None

    with pytest.raises(RuntimeError, match="No nonce set"):
        cal_mod.verify_assistive_touch(arm, at, MagicMock(), cal, _identity_pct_to_grbl())


def test_verify_assistive_touch_returns_failed_dict_on_screenshot_timeout(mocker) -> None:
    mocker.patch.object(cal_mod.time, "sleep")
    arm = MagicMock()
    at = MagicMock()
    at.at_screen = (0.05, 0.1)
    cal = _make_cal()
    cal._screenshot_nonce = [1, 0, 1, 0]
    bridge = MagicMock()
    bridge.wait_screenshot.return_value = None  # timeout

    out = cal_mod.verify_assistive_touch(
        arm, at, bridge, cal, _identity_pct_to_grbl(),
    )

    assert out["passed"] is False
    assert out["screenshot"]["passed"] is False
    assert out["clipboard"]["fetched"] is False


def test_verify_assistive_touch_returns_failed_dict_on_decode_error(mocker) -> None:
    mocker.patch.object(cal_mod.time, "sleep")
    arm = MagicMock()
    at = MagicMock()
    at.at_screen = (0.05, 0.1)
    cal = _make_cal()
    cal._screenshot_nonce = [1, 0, 1, 0]
    bridge = MagicMock()
    bridge.wait_screenshot.return_value = b"not an image"

    out = cal_mod.verify_assistive_touch(
        arm, at, bridge, cal, _identity_pct_to_grbl(),
    )

    assert out["passed"] is False


def test_verify_assistive_touch_raises_when_viewport_shift_unset(mocker) -> None:
    mocker.patch.object(cal_mod.time, "sleep")
    arm = MagicMock()
    at = MagicMock()
    at.at_screen = (0.05, 0.1)
    cal = _make_cal()
    cal._screenshot_nonce = [1, 0, 1, 0]
    cal.viewport_shift = None
    bridge = MagicMock()
    img = np.zeros((400, 400, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".png", img)
    bridge.wait_screenshot.return_value = buf.tobytes()

    with pytest.raises(RuntimeError, match="viewport_shift not set"):
        cal_mod.verify_assistive_touch(arm, at, bridge, cal, _identity_pct_to_grbl())


def test_verify_assistive_touch_full_success_path(mocker) -> None:
    mocker.patch.object(cal_mod.time, "sleep")
    mocker.patch.object(
        cal_mod.random, "randbytes", return_value=b"\xab\xcd\xef",
    )
    arm = MagicMock()
    at = MagicMock()
    at.at_screen = (0.05, 0.1)
    vshift = ViewportShift(
        offset_x=0, offset_y=0, dpr=3.0,
        screenshot_width=1170, screenshot_height=2532,
    )
    cal = _make_cal(viewport_shift=vshift)
    cal._screenshot_nonce = [1, 0, 1, 0]
    bridge = MagicMock()
    bridge.wait_clipboard.return_value = True
    img = np.zeros((400, 400, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".png", img)
    bridge.wait_screenshot.return_value = buf.tobytes()

    mocker.patch.object(cal_mod, "verify_nonce", return_value=(True, 4))

    out = cal_mod.verify_assistive_touch(
        arm, at, bridge, cal, _identity_pct_to_grbl(),
    )

    assert out["passed"] is True
    assert out["screenshot"]["passed"] is True
    assert out["clipboard"]["fetched"] is True
    assert out["clipboard"]["text"].startswith("PhysiClaw-")


def test_verify_assistive_touch_clipboard_timeout(mocker) -> None:
    mocker.patch.object(cal_mod.time, "sleep")
    mocker.patch.object(cal_mod.random, "randbytes", return_value=b"\x01\x02\x03")
    arm = MagicMock()
    at = MagicMock()
    at.at_screen = (0.05, 0.1)
    vshift = ViewportShift(
        offset_x=0, offset_y=0, dpr=3.0,
        screenshot_width=1170, screenshot_height=2532,
    )
    cal = _make_cal(viewport_shift=vshift)
    cal._screenshot_nonce = [1, 0, 1, 0]
    bridge = MagicMock()
    bridge.wait_clipboard.return_value = False
    img = np.zeros((400, 400, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".png", img)
    bridge.wait_screenshot.return_value = buf.tobytes()
    mocker.patch.object(cal_mod, "verify_nonce", return_value=(True, 4))

    out = cal_mod.verify_assistive_touch(
        arm, at, bridge, cal, _identity_pct_to_grbl(),
    )

    # Screenshot passed but clipboard didn't → overall failure.
    assert out["passed"] is False
    assert out["screenshot"]["passed"] is True
    assert out["clipboard"]["fetched"] is False
