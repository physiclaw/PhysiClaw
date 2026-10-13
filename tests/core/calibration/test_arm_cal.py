"""Tests for `physiclaw.core.calibration.arm_cal` — probe triangle,
grid fit, viewport-shift conversion, and the tilt diagnostic
(`_tilt_from_affine`). `calibrate_arm` is driven by patching
`arm_cal._tap_and_read` — the reference the loop actually looks up;
the shared tap helpers themselves are covered in test__common.py.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from physiclaw.core.bridge import CalibrationState
from physiclaw.core.calibration import arm_cal as arm_cal_mod
from physiclaw.core.calibration.arm_cal import _tilt_from_affine
from physiclaw.core.calibration.transforms import ViewportShift
from tests.core.calibration.conftest import make_cal as _make_cal

# ---------- _tilt_from_affine ----------


def test_tilt_from_affine_zero_for_aligned_diagonal_matrix() -> None:
    # arm-x maps onto screen-x only — no off-axis component → tilt 0.
    pct_to_grbl = np.array([[100.0, 0.0, 0.0], [0.0, 200.0, 0.0]])

    assert _tilt_from_affine(pct_to_grbl) == 0.0


def test_tilt_from_affine_one_for_perfectly_diagonal_arm_axis() -> None:
    # arm-x rotated 45° relative to screen — equal projection on both
    # screen axes → tilt 1.
    pct_to_grbl = np.array([[1.0, -1.0, 0.0], [1.0, 1.0, 0.0]])

    assert _tilt_from_affine(pct_to_grbl) == pytest.approx(1.0)


def test_tilt_from_affine_one_for_singular_matrix() -> None:
    # rank-1 — np.linalg.inv raises LinAlgError; helper returns 1.0.
    pct_to_grbl = np.array([[1.0, 1.0, 0.0], [2.0, 2.0, 0.0]])

    assert _tilt_from_affine(pct_to_grbl) == 1.0


def test_tilt_from_affine_intermediate_value_for_partial_tilt() -> None:
    # Rotation of arm-x by ~26.6° → tan(26.6°) ≈ 0.5. The minor/major
    # ratio of the inverted basis comes out close to that.
    angle = np.radians(26.565)
    R = np.array([[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]])
    pct_to_grbl = np.zeros((2, 3))
    pct_to_grbl[:, :2] = R

    tilt = _tilt_from_affine(pct_to_grbl)

    # 26.565° → 0.5 ratio. Allow ±0.05 for floating-point.
    assert tilt == pytest.approx(0.5, abs=0.05)


def test_tilt_from_affine_returns_one_when_major_axis_near_zero(
    mocker,
) -> None:
    # Edge case: A_inv computes successfully but the arm-x basis vector
    # is essentially zero. We synthesize this by patching np.linalg.inv
    # to return a degenerate row.
    fake_inv = np.array([[1e-9, 1.0], [1e-9, 1.0]])
    mocker.patch("numpy.linalg.inv", return_value=fake_inv)

    assert _tilt_from_affine(np.eye(2, 3)) == 1.0


# ---------- calibrate_arm ----------


def test_calibrate_arm_succeeds(mocker) -> None:
    mocker.patch.object(arm_cal_mod.time, "sleep")
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
        {"x": col, "y": row} for row in cal.GRID_ROWS_PCT for col in cal.GRID_COLS_PCT
    ]

    # Mock _tap_and_read directly to bypass the inner flush_touches loop.
    tap_returns = iter(probe_touches + grid_touches)
    mocker.patch.object(
        arm_cal_mod, "_tap_and_read", side_effect=lambda *a, **kw: next(tap_returns)
    )

    pct_to_grbl, tilt, gtouches = arm_cal_mod.calibrate_arm(arm, cal)

    assert pct_to_grbl.shape == (2, 3)
    assert isinstance(tilt, float)
    assert len(gtouches) == 15
    arm.set_origin.assert_called_once()


def test_calibrate_arm_raises_on_probe_center_miss(mocker) -> None:
    mocker.patch.object(arm_cal_mod.time, "sleep")
    arm = MagicMock()
    cal = _make_cal()
    mocker.patch.object(arm_cal_mod, "_tap_and_read", side_effect=[None])

    with pytest.raises(RuntimeError, match="no touch at center"):
        arm_cal_mod.calibrate_arm(arm, cal)


def test_calibrate_arm_raises_on_probe_x_miss(mocker) -> None:
    mocker.patch.object(arm_cal_mod.time, "sleep")
    arm = MagicMock()
    cal = _make_cal()
    mocker.patch.object(
        arm_cal_mod,
        "_tap_and_read",
        side_effect=[{"x": 0.5, "y": 0.5}, None],
    )

    with pytest.raises(RuntimeError, match="no touch at \\+10mm X"):
        arm_cal_mod.calibrate_arm(arm, cal)


def test_calibrate_arm_raises_on_probe_y_miss(mocker) -> None:
    mocker.patch.object(arm_cal_mod.time, "sleep")
    arm = MagicMock()
    cal = _make_cal()
    mocker.patch.object(
        arm_cal_mod,
        "_tap_and_read",
        side_effect=[
            {"x": 0.5, "y": 0.5},
            {"x": 0.6, "y": 0.5},
            None,
        ],
    )

    with pytest.raises(RuntimeError, match="no touch at \\+10mm Y"):
        arm_cal_mod.calibrate_arm(arm, cal)


def test_calibrate_arm_raises_when_too_few_grid_hits(mocker) -> None:
    mocker.patch.object(arm_cal_mod.time, "sleep")
    arm = MagicMock()
    cal = _make_cal()
    # 3 probes succeed, all 15 grid taps miss (None) — only 3 valid pairs.
    probe = [
        {"x": 0.5, "y": 0.5},
        {"x": 0.6, "y": 0.5},
        {"x": 0.5, "y": 0.6},
    ]
    grid_misses = [None] * 15
    mocker.patch.object(
        arm_cal_mod,
        "_tap_and_read",
        side_effect=probe + grid_misses,
    )

    with pytest.raises(RuntimeError, match="only 3 valid taps"):
        arm_cal_mod.calibrate_arm(arm, cal)


def test_calibrate_arm_uses_viewport_pct_when_shift_set(mocker) -> None:
    mocker.patch.object(arm_cal_mod.time, "sleep")
    arm = MagicMock()
    vshift = ViewportShift(
        offset_x=0,
        offset_y=0,
        dpr=3.0,
        screenshot_width=1170,
        screenshot_height=2532,
    )
    cal = _make_cal(viewport_shift=vshift)
    cal.viewport_pct_to_screenshot_pct.side_effect = lambda c, r: (c, r)

    probe = [
        {"x": 0.5, "y": 0.5},
        {"x": 0.6, "y": 0.5},
        {"x": 0.5, "y": 0.6},
    ]
    grid = [
        {"x": col, "y": row} for row in cal.GRID_ROWS_PCT for col in cal.GRID_COLS_PCT
    ]
    mocker.patch.object(arm_cal_mod, "_tap_and_read", side_effect=probe + grid)

    arm_cal_mod.calibrate_arm(arm, cal)

    # viewport_pct_to_screenshot_pct was used for grid prediction.
    assert cal.viewport_pct_to_screenshot_pct.call_count == 15


def test_calibrate_arm_requires_viewport_shift(mocker) -> None:
    """Without the shift the fit would land in raw viewport 0-1 —
    numerically valid and wrong — so it must refuse to run."""
    mocker.patch.object(arm_cal_mod.time, "sleep")
    cal = _make_cal(viewport_shift=None)

    with pytest.raises(RuntimeError, match="Viewport shift not measured"):
        arm_cal_mod.calibrate_arm(MagicMock(), cal)


class _ReportingArm:
    """Device boundary: report touches from physical position through the bridge."""

    def __init__(self, cal, affine, reports=None):
        self.cal = cal
        self.affine = affine.copy()
        self.reports = None if reports is None else iter(reports)
        self.position = np.zeros(2)
        self.moves = []
        self.origin_resets = []
        self.solenoid = SimpleNamespace(tap=self._report)

    def rapid_to(self, x, y):
        self.position = np.array([x, y])
        self.moves.append((x, y))

    def wait_idle(self):
        pass

    def return_to_origin(self):
        self.rapid_to(0, 0)

    def set_origin(self):
        self.origin_resets.append(self.position.copy())
        self.affine[:, 2] -= self.position
        self.position = np.zeros(2)

    def _report(self, _duration):
        if self.reports is None:
            xy = np.linalg.solve(self.affine[:, :2], self.position - self.affine[:, 2])
        else:
            xy = next(self.reports, None)
        if xy is not None:
            self.cal.report_touch({"x": float(xy[0]), "y": float(xy[1])})


def _physical_affine(width=70.0, height=140.0, angle=0.0):
    c, s = np.cos(angle), np.sin(angle)
    linear = np.array([[width * c, -height * s], [width * s, height * c]])
    return np.c_[linear, -linear @ np.array([0.5, 0.5])]


def _reporting_setup(mocker, *, reports=None, affine=None):
    mocker.patch.object(arm_cal_mod.time, "sleep")
    cal = CalibrationState()
    cal.viewport_shift = ViewportShift(0, 0, 1, 600, 900)
    cal.screen_dimension = {"viewport_width": 600, "viewport_height": 900}
    physical = _physical_affine() if affine is None else affine
    return _ReportingArm(cal, physical, reports), cal


@pytest.mark.parametrize(
    "probes",
    [
        [(0.5, 0.5), (0.5, 0.5), (0.5, 0.6)],
        [(0.5, 0.5), (0.6, 0.5), (0.7, 0.5)],
    ],
)
def test_regression_invalid_probe_fit_never_moves_to_grid(mocker, probes):
    arm, cal = _reporting_setup(mocker, reports=probes)

    with pytest.raises(RuntimeError, match="probe affine fit is unusable"):
        arm_cal_mod.calibrate_arm(arm, cal)

    assert arm.moves == [(0, 0), (10, 0), (0, 10)]
    assert arm.origin_resets == []


@pytest.mark.parametrize(
    "probes",
    [
        [(0.5, 0.5), (0.501, 0.5), (0.5, 0.6)],
        [(0.5, 0.5), (0.6, 0.5), (0.5, 0.501)],
        [(0.01, 0.5), (0.01 + 10 / 210, 0.5), (0.01, 0.6)],
        [(0.5, 0.5), (0.525, 0.5), (0.5, 0.6)],
    ],
)
def test_regression_impossible_probe_trajectory_rejected_before_grid(mocker, probes):
    arm, cal = _reporting_setup(mocker, reports=probes)

    with pytest.raises(RuntimeError, match="gantry travel"):
        arm_cal_mod.calibrate_arm(arm, cal)

    assert arm.moves == [(0, 0), (10, 0), (0, 10)]
    assert cal.phase == "center"
    assert arm.origin_resets == []


def test_regression_unrelated_grid_touches_cannot_certify_final_fit(mocker):
    probes = [(0.5, 0.5), (0.6, 0.5), (0.5, 0.6)]
    outliers = np.random.default_rng(0).uniform(0.05, 0.95, (15, 2))
    arm, cal = _reporting_setup(mocker, reports=[*probes, *outliers])

    with pytest.raises(RuntimeError, match="inlier taps"):
        arm_cal_mod.calibrate_arm(arm, cal)

    assert arm.origin_resets == []
    assert len(arm.moves) == 19  # 18 taps, then a return to the known origin


def _replace_final_fit(mocker, affine, mask):
    estimate = arm_cal_mod.cv2.estimateAffine2D
    calls = 0

    def fit(screen, positions):
        nonlocal calls
        calls += 1
        return estimate(screen, positions) if calls == 1 else (affine, mask)

    mocker.patch.object(arm_cal_mod.cv2, "estimateAffine2D", side_effect=fit)


@pytest.mark.parametrize(
    "mask", [None, np.zeros((18, 1)), np.r_[np.ones(5), np.zeros(13)]]
)
def test_arm_final_fit_requires_six_actual_inliers(mocker, mask):
    arm, cal = _reporting_setup(mocker)
    _replace_final_fit(mocker, _physical_affine(), mask)

    with pytest.raises(RuntimeError, match="inlier taps"):
        arm_cal_mod.calibrate_arm(arm, cal)

    assert arm.origin_resets == []


def test_arm_final_fit_accepts_six_inliers(mocker):
    arm, cal = _reporting_setup(mocker)
    _replace_final_fit(mocker, _physical_affine(), np.r_[np.ones(6), np.zeros(12)])

    affine, _, _ = arm_cal_mod.calibrate_arm(arm, cal)

    np.testing.assert_allclose(affine, _physical_affine())
    assert len(arm.origin_resets) == 1


@pytest.mark.parametrize("affine", [None, np.zeros((2, 3)), np.full((2, 3), np.nan)])
def test_arm_final_fit_rejected_before_resetting_origin(mocker, affine):
    arm, cal = _reporting_setup(mocker)
    _replace_final_fit(mocker, affine, np.ones((18, 1)))

    with pytest.raises(RuntimeError, match="final affine fit"):
        arm_cal_mod.calibrate_arm(arm, cal)

    assert arm.origin_resets == []
    assert len(arm.moves) == 19


def test_arm_final_center_cannot_exceed_mechanical_travel(mocker):
    arm, cal = _reporting_setup(mocker)
    affine = _physical_affine()
    affine[:, 2] += 1000
    _replace_final_fit(mocker, affine, np.ones((18, 1)))

    with pytest.raises(RuntimeError, match="gantry travel"):
        arm_cal_mod.calibrate_arm(arm, cal)

    assert arm.origin_resets == []
    assert len(arm.moves) == 19


def test_regression_final_scale_checked_even_when_center_stays_near_zero(mocker):
    arm, cal = _reporting_setup(mocker, affine=_physical_affine(100, 100))
    report = arm.solenoid.tap
    calls = 0

    def report_collapsed_grid(duration):
        nonlocal calls
        calls += 1
        if calls <= 3:
            report(duration)
        else:
            x, y = 0.5 + arm.position / 1_000_000
            cal.report_touch({"x": x, "y": y})

    arm.solenoid.tap = report_collapsed_grid

    with pytest.raises(RuntimeError, match="gantry travel"):
        arm_cal_mod.calibrate_arm(arm, cal)

    assert arm.origin_resets == []
    assert len(arm.moves) == 19


@settings(
    max_examples=15,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(width=st.floats(40, 90), height=st.floats(80, 170), angle=st.floats(-0.04, 0.04))
def test_arm_fit_recovers_physical_mapping_and_centers_origin(
    mocker, width, height, angle
):
    expected = _physical_affine(width, height, angle)
    arm, cal = _reporting_setup(mocker, affine=expected)

    affine, _, touches = arm_cal_mod.calibrate_arm(arm, cal)

    np.testing.assert_allclose(affine, expected, atol=1e-4)
    np.testing.assert_allclose(affine @ [0.5, 0.5, 1], [0, 0], atol=1e-10)
    np.testing.assert_allclose(arm.origin_resets, [[0, 0]], atol=1e-4)
    assert len(touches) == 15


def test_arm_fit_includes_offscreen_park_in_mechanical_travel(mocker):
    arm, cal = _reporting_setup(mocker, affine=_physical_affine(105, 140))

    with pytest.raises(RuntimeError, match="gantry travel"):
        arm_cal_mod.calibrate_arm(arm, cal)

    assert arm.origin_resets == []
    assert len(arm.moves) == 19


def test_arm_fit_rebases_an_off_center_probe_origin(mocker):
    expected = _physical_affine()
    initial = expected.copy()
    initial[:, 2] += (5, -4)
    arm, cal = _reporting_setup(mocker, affine=initial)

    affine, _, touches = arm_cal_mod.calibrate_arm(arm, cal)

    np.testing.assert_allclose(affine, expected, atol=1e-4)
    np.testing.assert_allclose(arm.origin_resets, [[5, -4]], atol=1e-4)
    assert len(touches) == 15
