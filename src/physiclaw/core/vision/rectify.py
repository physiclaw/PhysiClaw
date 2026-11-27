"""Resample the calibrated screen plane into logical screen coordinates."""

import cv2
import numpy as np


def rectify_screen(
    frame: np.ndarray,
    mapping: np.ndarray,
    size: tuple[int, int],
    *,
    camera_size: tuple[int, int],
) -> np.ndarray:
    """Output pixel (x, y) represents screen point (x / width, y / height).

    The saved map is affine: this corrects rotation, scale and shear, but
    cannot correct perspective that the calibration did not measure.
    Never fill a missing screen edge with plausible-looking black pixels.
    """
    width, height = size
    if width <= 0 or height <= 0:
        raise ValueError("Screen dimensions must be positive")
    frame_h, frame_w = frame.shape[:2]
    cal_w, cal_h = camera_size
    if abs(frame_w * cal_h - cal_w * frame_h) > 0.01 * cal_w * frame_h:
        raise ValueError("Camera aspect changed — recalibrate")
    matrix = np.asarray(mapping, dtype=np.float64)[:2].copy()
    if not np.isfinite(matrix).all() or abs(np.linalg.det(matrix[:, :2])) < 1e-8:
        raise ValueError("Invalid screen calibration")
    corners = matrix @ np.array([[0, 1, 1, 0], [0, 0, 1, 1], [1, 1, 1, 1]])
    if (corners < 0).any() or (corners > 1).any():
        raise ValueError("Calibrated screen extends outside camera frame")
    matrix *= np.array([[frame_w], [frame_h]])
    matrix[:, 0] /= width
    matrix[:, 1] /= height
    return cv2.warpAffine(
        frame,
        matrix,
        size,
        flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
    )
