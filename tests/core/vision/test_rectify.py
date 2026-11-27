"""Geometric regression: sampled pixels and physical points must agree."""

import numpy as np
import pytest

from physiclaw.core.calibration.transforms import ScreenTransforms
from physiclaw.core.vision.rectify import rectify_screen


def test_rectification_matches_calibrated_points_including_edges():
    # Encode camera x/y in image channels; interpolation preserves this ramp.
    y, x = np.indices((200, 200))
    camera = np.stack([x, y, np.zeros_like(x)], axis=-1).astype(np.uint8)
    t = ScreenTransforms(
        np.array([[50, 2, 10], [-1, 100, 20]]),
        np.array([[0.6, 0.1, 0.1], [-0.1, 0.7, 0.2]]),
        (200, 200),
    )
    screen = rectify_screen(camera, t.pct_to_cam, (80, 160), camera_size=t.cam_size)
    for px, py in [(1, 1), (79, 1), (79, 159), (1, 159), (40, 80)]:
        pct = np.array([px / 80, py / 160, 1])
        expected = (t.pct_to_cam @ pct) * 200
        np.testing.assert_allclose(screen[py, px, :2], expected, atol=1)
        # Same normalized point is what the existing arm transform consumes.
        np.testing.assert_allclose(t.pct_to_grbl_mm(*pct[:2]), t.pct_to_grbl @ pct)


@pytest.mark.parametrize(
    "matrix,match",
    [
        ([[1, 0, 0.1], [0, 1, 0]], "outside"),
        ([[0, 0, 0], [0, 0, 0]], "Invalid"),
        ([[float("nan"), 0, 0], [0, 1, 0]], "Invalid"),
    ],
)
def test_rejects_missing_screen_or_invalid_mapping(matrix, match):
    t = ScreenTransforms(np.eye(2, 3), np.array(matrix), (100, 200))
    with pytest.raises(ValueError, match=match):
        rectify_screen(
            np.zeros((200, 100, 3), np.uint8),
            t.pct_to_cam,
            (100, 200),
            camera_size=t.cam_size,
        )


def test_rejects_changed_camera_aspect():
    t = ScreenTransforms(np.eye(2, 3), np.eye(2, 3), (100, 200))
    with pytest.raises(ValueError, match="aspect"):
        rectify_screen(
            np.zeros((100, 200, 3), np.uint8),
            t.pct_to_cam,
            (100, 200),
            camera_size=t.cam_size,
        )
