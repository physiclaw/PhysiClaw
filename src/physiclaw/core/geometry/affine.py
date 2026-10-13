"""Affine point math — the coordinate-frame primitives.

Domain-free: nothing here knows about screens, arms, or cameras. The
sibling ``screen`` module holds the domain constants; both live in this
leaf package because ``hardware`` (the AssistiveTouch driver applies
the arm affine) must reach them without importing ``calibration``.
"""

import numpy as np


def validate_affine(affine: np.ndarray) -> None:
    """Reject malformed, non-finite or degenerate (2, 3) point mappings.

    Validate once when loading or fitting a mapping, before using it for
    motion or caching its inverse. Reflections and off-screen offsets are valid.
    """
    if affine.shape != (2, 3) or not np.isfinite(affine).all():
        raise ValueError("affine must be a finite 2-by-3 matrix")
    if np.linalg.matrix_rank(affine[:, :2]) != 2:
        raise ValueError("affine must have an invertible linear part")
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        inverse = invert_affine(affine)
    if not np.isfinite(inverse).all():
        raise ValueError("affine inverse must be finite")


def apply_affine(affine: np.ndarray, x: float, y: float) -> tuple[float, float]:
    """Apply a (2, 3) affine to a point — THE point-mapping primitive.

    Every screen↔arm↔camera point conversion goes through here (mostly
    via ``ScreenTransforms``); nothing else in the codebase multiplies a
    coordinate by an affine, so a change to the convention (a y-flip, a
    perspective term) is a one-place edit."""
    pt = affine @ np.array([x, y, 1.0])
    return (float(pt[0]), float(pt[1]))


def invert_affine(affine: np.ndarray) -> np.ndarray:
    """The (2, 3) affine mapping the other direction.

    The single inverse implementation — e.g. Mapping B (screen 0-1 →
    camera 0-1) inverted for back-projection during validation."""
    A = affine[:, :2]
    b = affine[:, 2]
    A_inv = np.linalg.inv(A)
    return np.hstack([A_inv, (-A_inv @ b).reshape(2, 1)])
