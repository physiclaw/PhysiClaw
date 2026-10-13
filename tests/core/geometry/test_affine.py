"""Affine acceptance and inversion invariants at the geometry boundary."""

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from physiclaw.core.geometry import apply_affine, invert_affine, validate_affine


@pytest.mark.parametrize(
    "value",
    [[], [[1, 0], [0, 1]], [[1, 0, np.nan], [0, 1, 0]], [[np.inf, 0, 0], [0, 1, 0]]],
)
def test_validate_affine_rejects_malformed_or_nonfinite_coefficients(value) -> None:
    affine = np.array(value)

    with pytest.raises(ValueError, match="finite 2-by-3"):
        validate_affine(affine)


@pytest.mark.parametrize("value", [[[0, 0, 1], [0, 0, 2]], [[1, 1, 0], [2, 2, 1]]])
def test_validate_affine_rejects_degenerate_linear_part(value) -> None:
    affine = np.array(value, dtype=float)

    with pytest.raises(ValueError, match="invertible linear part"):
        validate_affine(affine)


@pytest.mark.parametrize(
    "value", [[[1e-320, 0, 0], [0, 1e-320, 0]], [[0.1, 0, 1e308], [0, 0.1, 1e308]]]
)
def test_regression_validate_affine_rejects_nonfinite_inverse(value) -> None:
    affine = np.array(value)

    with pytest.raises(ValueError, match="inverse must be finite"):
        validate_affine(affine)


@given(
    x_scale=st.floats(0.01, 1000),
    y_scale=st.floats(-1000, -0.01),
    shear=st.floats(-1, 1),
    offset=st.tuples(st.floats(-1000, 1000), st.floats(-1000, 1000)),
    point=st.tuples(st.floats(-10, 10), st.floats(-10, 10)),
)
def test_valid_affine_inverse_round_trips_reflected_sheared_points(
    x_scale, y_scale, shear, offset, point
) -> None:
    affine = np.array([[x_scale, shear, offset[0]], [0, y_scale, offset[1]]])

    validate_affine(affine)
    restored = apply_affine(invert_affine(affine), *apply_affine(affine, *point))

    assert restored == pytest.approx(point, abs=1e-6)
