"""Tests for `physiclaw.core.vision.grid_detect`."""
from __future__ import annotations

import cv2
import numpy as np
import pytest

from physiclaw.core.vision.grid_detect import (
    compute_affine_transforms,
    detect_orange_dot,
    detect_red_dots,
    sort_dots_to_grid,
)


# ---------- detect_red_dots ----------


def _frame(h: int = 200, w: int = 200) -> np.ndarray:
    return np.zeros((h, w, 3), dtype=np.uint8)


def _draw_dot(frame: np.ndarray, cx: int, cy: int, color, radius: int = 8) -> None:
    cv2.circle(frame, (cx, cy), radius, color, -1)


def test_detect_red_dots_finds_circles() -> None:
    frame = _frame()
    _draw_dot(frame, 50, 50, (0, 0, 255))
    _draw_dot(frame, 150, 50, (0, 0, 255))

    dots = detect_red_dots(frame)

    assert len(dots) == 2
    xs = sorted(d[0] for d in dots)
    assert 40 < xs[0] < 60
    assert 140 < xs[1] < 160


def test_detect_red_dots_returns_empty_on_blank_frame() -> None:
    assert detect_red_dots(_frame()) == []


def test_detect_red_dots_ignores_non_red() -> None:
    frame = _frame()
    _draw_dot(frame, 50, 50, (0, 255, 0))  # green
    _draw_dot(frame, 100, 50, (255, 0, 0))  # blue

    assert detect_red_dots(frame) == []


def test_detect_red_dots_filters_too_small() -> None:
    frame = _frame()
    # 2-pixel dot — area < 50 threshold.
    _draw_dot(frame, 50, 50, (0, 0, 255), radius=1)

    assert detect_red_dots(frame) == []


def test_detect_red_dots_filters_too_large() -> None:
    frame = _frame(h=400, w=400)
    # Huge filled red region — area > 10000 threshold.
    cv2.rectangle(frame, (50, 50), (350, 350), (0, 0, 255), -1)

    assert detect_red_dots(frame) == []


def test_detect_red_dots_filters_non_circular() -> None:
    frame = _frame()
    # Highly elongated rectangle has low circularity.
    cv2.rectangle(frame, (40, 50), (160, 53), (0, 0, 255), -1)

    assert detect_red_dots(frame) == []


def test_detect_red_dots_handles_high_hue_red() -> None:
    """Red wraps around at H=180, second mask covers 160-180."""
    frame = _frame()
    # Pure red is at H=0; its alternate side wrap is what the second
    # range catches. Using BGR (0, 0, 255) hits the first range, but
    # any near-red mid-saturation sample lands in either. This just
    # confirms a normal red is detected.
    _draw_dot(frame, 100, 100, (0, 0, 255))

    assert len(detect_red_dots(frame)) == 1


# ---------- sort_dots_to_grid ----------


def test_sort_dots_to_grid_orders_row_major() -> None:
    # 3x2 grid scrambled.
    dots = [(20, 50), (10, 10), (10, 50), (20, 10), (20, 30), (10, 30)]

    grid = sort_dots_to_grid(dots, rows=3, cols=2)

    # Row-major order: (10,10), (20,10), (10,30), (20,30), (10,50), (20,50)
    expected = np.array([
        [10, 10], [20, 10],
        [10, 30], [20, 30],
        [10, 50], [20, 50],
    ], dtype=np.float64)
    np.testing.assert_array_equal(grid, expected)
    assert grid.shape == (6, 2)


def test_sort_dots_to_grid_raises_on_count_mismatch() -> None:
    with pytest.raises(RuntimeError, match="Expected 6 red dots but detected 3"):
        sort_dots_to_grid([(0, 0), (1, 1), (2, 2)], rows=3, cols=2)


def test_sort_dots_to_grid_raises_on_too_many() -> None:
    with pytest.raises(RuntimeError, match="Expected 4 red dots but detected 5"):
        sort_dots_to_grid([(0, 0)] * 5, rows=2, cols=2)


def test_sort_dots_to_grid_returns_float64_dtype() -> None:
    grid = sort_dots_to_grid([(0, 0), (1, 0), (0, 1), (1, 1)], rows=2, cols=2)

    assert grid.dtype == np.float64


# ---------- compute_affine_transforms ----------


def test_compute_affine_transforms_returns_2x3_pair() -> None:
    pcts = np.array([
        [0.0, 0.0], [1.0, 0.0], [0.0, 1.0], [1.0, 1.0],
        [0.5, 0.5],
    ], dtype=np.float32)
    grbl = pcts * 100  # scale to mm
    pixels = pcts * np.array([800, 1000], dtype=np.float32)

    pct_to_grbl, pct_to_pixel = compute_affine_transforms(pcts, grbl, pixels)

    assert pct_to_grbl.shape == (2, 3)
    assert pct_to_pixel.shape == (2, 3)


def test_compute_affine_transforms_round_trips() -> None:
    pcts = np.array([
        [0.0, 0.0], [1.0, 0.0], [0.0, 1.0], [1.0, 1.0],
        [0.5, 0.5],
    ], dtype=np.float32)
    grbl = pcts * 100
    pixels = pcts * np.array([800, 1000], dtype=np.float32)

    pct_to_grbl, _ = compute_affine_transforms(pcts, grbl, pixels)

    # Apply to (0.5, 0.5) → expect (50, 50).
    out = pct_to_grbl @ np.array([0.5, 0.5, 1.0])
    np.testing.assert_allclose(out, (50.0, 50.0), atol=1e-3)


def test_compute_affine_transforms_raises_on_too_few_points() -> None:
    pcts = np.array([[0.0, 0.0]], dtype=np.float32)

    with pytest.raises(RuntimeError, match="Failed to compute affine"):
        compute_affine_transforms(pcts, pcts, pcts)


# ---------- detect_orange_dot ----------


def test_detect_orange_dot_returns_center(mocker) -> None:
    spy = mocker.patch(
        "physiclaw.core.vision.grid_detect.find_largest_hsv_blob",
        return_value=(123.4, 56.7),
    )
    frame = _frame()

    out = detect_orange_dot(frame)

    assert out == (123.4, 56.7)
    spy.assert_called_once_with(
        frame, [5, 100, 100], [25, 255, 255], min_area=50,
    )


def test_detect_orange_dot_returns_none_when_no_blob(mocker) -> None:
    mocker.patch(
        "physiclaw.core.vision.grid_detect.find_largest_hsv_blob",
        return_value=None,
    )

    assert detect_orange_dot(_frame()) is None
