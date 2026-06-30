"""
Grid + dot detection — pure CV helpers used during calibration.

Detects red calibration dots (3×5 grid) and orange validation dots in
camera frames. Also provides the affine transform helper that maps screen
percentages to GRBL mm and camera pixels.

Pure functions: frame in → results out. No hardware dependency.
"""

import logging

import cv2
import numpy as np

from physiclaw.core.vision.util import (
    contour_centroid,
    find_largest_hsv_blob,
    redness,
)

log = logging.getLogger(__name__)

# R - max(G, B) floor for a calibration dot. Dots are small and render on a
# bright white screen, so the camera desaturates them to a dim pink that an
# HSV saturation floor misses entirely — but their red channel still leads.
# Kept generous (catch faint dots even in a dim room); the median-area trim
# in detect_red_dots then drops the extra glare/noise blobs it lets through.
RED_DOT_MIN_REDNESS = 30

# Contour filters for a calibration dot (small, round).
DOT_MIN_AREA = 50
DOT_MAX_AREA = 10000
DOT_MIN_CIRCULARITY = 0.5


# ─── Red dot detection ────────────────────────────────────────


def detect_red_dots(
    frame: np.ndarray, expected: int | None = None
) -> list[tuple[float, float]]:
    """Detect red calibration dots; return (cx, cy) pixel centers.

    Dots are isolated by a redness floor (``R - max(G, B)``) rather than HSV
    saturation, because small dots on a bright screen desaturate to dim pink
    that a saturation floor would miss.

    When ``expected`` is given and more blobs pass the filters than expected,
    the ``expected`` whose area is closest to the median are kept. The grid
    dots are uniform in size, so glare / merged / noise blobs (area outliers)
    fall away — far more robust than betting on one exact threshold.
    """
    mask = (redness(frame) >= RED_DOT_MIN_REDNESS).astype(np.uint8) * 255

    # Clean up noise
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    cands: list[tuple[float, float, float]] = []  # (cx, cy, area)
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < DOT_MIN_AREA or area > DOT_MAX_AREA:
            continue
        perimeter = cv2.arcLength(cnt, True)
        if perimeter == 0:
            continue
        if 4 * np.pi * area / (perimeter * perimeter) < DOT_MIN_CIRCULARITY:
            continue
        c = contour_centroid(cnt)
        if c is None:
            continue
        cands.append((c[0], c[1], area))

    if expected is not None and len(cands) > expected:
        med = float(np.median([a for *_, a in cands]))
        cands = sorted(cands, key=lambda c: abs(c[2] - med))[:expected]

    log.debug(f"Detected {len(cands)} red dots")
    return [(cx, cy) for cx, cy, _ in cands]


def sort_dots_to_grid(
    dots: list[tuple[float, float]], rows: int, cols: int
) -> np.ndarray:
    """Sort detected dot centroids into row-major grid order.

    Returns shape (rows*cols, 2) array.
    Raises RuntimeError if dot count doesn't match expected grid.
    """
    expected = rows * cols
    if len(dots) != expected:
        raise RuntimeError(
            f"Expected {expected} red dots but detected {len(dots)}. "
            f"Check lighting, camera focus, and that the grid page is displayed."
        )

    # Sort by Y to group into rows
    dots_sorted = sorted(dots, key=lambda d: d[1])

    grid = []
    for r in range(rows):
        row_dots = dots_sorted[r * cols : (r + 1) * cols]
        # Sort by X within each row
        row_dots.sort(key=lambda d: d[0])
        grid.extend(row_dots)

    return np.array(grid, dtype=np.float64)


# ─── Affine transform computation ─────────────────────────────


def compute_affine_transforms(
    screen_pcts: np.ndarray,
    grbl_positions: np.ndarray,
    camera_pixels: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Compute affine transforms using RANSAC for robustness.

    Args:
        screen_pcts: (N, 2) screen coordinates as 0-1 decimals (x, y)
        grbl_positions: (N, 2) GRBL mm positions
        camera_pixels: (N, 2) camera pixel positions

    Returns:
        (pct_to_grbl, pct_to_pixel) — each is a (2, 3) affine matrix.
        Apply via: [x_out, y_out] = M @ [x_in, y_in, 1]
    """
    pct_to_grbl, _ = cv2.estimateAffine2D(screen_pcts, grbl_positions)
    pct_to_pixel, _ = cv2.estimateAffine2D(screen_pcts, camera_pixels)

    if pct_to_grbl is None or pct_to_pixel is None:
        raise RuntimeError(
            "Failed to compute affine transforms — not enough valid calibration points"
        )

    return pct_to_grbl, pct_to_pixel


# ─── Orange dot detection (for validation) ───────────────────


def detect_orange_dot(frame: np.ndarray) -> tuple[float, float] | None:
    """Detect a single orange dot in a camera frame.

    Returns (cx, cy) in camera pixels, or None if not found.
    Orange #f97316 ≈ HSV H=20°, S=90%, V=97% → OpenCV H=10, S=230, V=247.
    """
    return find_largest_hsv_blob(
        frame, [5, 100, 100], [25, 255, 255], min_area=50
    )
