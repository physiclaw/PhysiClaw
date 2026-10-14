"""Tests for `physiclaw.common.bbox` — the shared core/agent bbox contract.

Accepted mutation survivor: decoration of parse_within's invalid-band error.
The rejection and accepted band coordinates are checked without pinning prose.
"""

from __future__ import annotations

import math
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from physiclaw.common.bbox import (
    center_of,
    inside,
    near,
    parse_box,
    parse_within,
    same_line,
    validate_bbox,
)


@pytest.mark.parametrize(
    "bbox",
    [None, "0,0,1,1", [0.1, 0.2, 0.9], [0.1, 0.2, 0.9, 1.0, 0.5], {}, 0.5],
)
def test_wrong_shape_raises(bbox: Any) -> None:
    with pytest.raises(
        ValueError, match=r"^bbox: must be \[left, top, right, bottom\];"
    ):
        validate_bbox(bbox)


@pytest.mark.parametrize(
    "bbox",
    [[0.1, "0.2", 0.9, 1.0], [0.1, None, 0.9, 1.0], [0.1, [0.2], 0.9, 1.0]],
)
def test_non_number_coord_raises(bbox: list) -> None:
    with pytest.raises(ValueError, match=r"^bbox: each coord must be a number;"):
        validate_bbox(bbox)


@pytest.mark.parametrize(
    "bbox",
    [[-0.1, 0.2, 0.9, 1.0], [0.1, 0.2, 1.1, 1.0], [0.1, -2, 0.9, 1.0]],
)
def test_out_of_unit_range_raises(bbox: list[float]) -> None:
    with pytest.raises(ValueError, match=r"^bbox: each coord must be in \[0, 1\];"):
        validate_bbox(bbox)


@pytest.mark.parametrize(
    "bbox",
    [
        [0.9, 0.2, 0.1, 1.0],
        [0.1, 1.0, 0.9, 0.2],
        [0.5, 0.2, 0.5, 1.0],
        [0.1, 0.5, 0.9, 0.5],
    ],
)
def test_inverted_or_degenerate_raises(bbox: list[float]) -> None:
    with pytest.raises(ValueError, match=r"^bbox: left < right, top < bottom;"):
        validate_bbox(bbox)


def test_valid_bbox_returned_unchanged() -> None:
    bbox = [0.1, 0.2, 0.9, 1.0]
    assert validate_bbox(bbox) is bbox


def test_tuple_accepted() -> None:
    bbox = (0.0, 0.0, 1.0, 1.0)
    assert validate_bbox(bbox) is bbox


@pytest.mark.parametrize(
    "name, expected",
    [
        ("top", (0, 0, 1, 0.25)),
        ("bottom", (0, 0.75, 1, 1)),
        ("left", (0, 0, 0.3, 1)),
        ("right", (0.7, 0, 1, 1)),
    ],
)
def test_named_regions_match_screen_bands(name, expected):
    assert parse_within(name) == expected


@pytest.mark.parametrize(
    "point, expected",
    [
        ((0.375, 0.75), True),
        ((0.25, 0.875), True),
        ((0.125, 0.625), True),
        ((0.376, 0.75), False),
        ((0.25, 0.876), False),
    ],
)
def test_near_includes_both_axes_at_the_tolerance_boundary(point, expected):
    assert near((0.25, 0.75), point, tolerance=0.125) is expected


@pytest.mark.parametrize(
    "other, expected",
    [
        ((0.8, 0.3, 1, 0.4), True),
        ((0.8, 0.5, 1, 0.75), False),
        ((0.8, 0, 1, 0.25), False),
    ],
)
def test_same_line_requires_vertical_overlap_not_touching(other, expected):
    assert same_line((0, 0.25, 0.1, 0.5), other) is expected


@pytest.mark.parametrize(
    "point, expected",
    [
        ((0.125, 0.125), True),
        ((0.875, 0.875), True),
        ((0.124, 0.5), False),
        ((0.876, 0.5), False),
        ((0.5, 0.124), False),
        ((0.5, 0.876), False),
    ],
)
def test_inside_includes_all_expanded_edges(point, expected):
    assert inside(point, (0.25, 0.25, 0.75, 0.75), margin=0.125) is expected


# ---------- cross-layer contract ----------
#
# Both layers must expose the SAME checks so the agent sees an identical
# diagnostic regardless of which side catches the violation. These pins
# turn a drifted re-export or a re-inlined copy into a red test.


def test_core_reexport_is_the_same_object() -> None:
    from physiclaw.core.vision import util

    assert util.validate_bbox is validate_bbox


@pytest.mark.parametrize(
    "bad",
    [[0.9, 0.2, 0.1, 1.0], [float("nan"), 0.2, 0.9, 1.0], [False, 0.2, 0.9, 1.0]],
)
def test_engine_validator_wraps_the_same_message(bad) -> None:
    from physiclaw.agent.engine.validator import ValidationError, validate_arguments

    with pytest.raises(ValueError) as common_err:
        validate_bbox(bad)
    with pytest.raises(ValidationError) as engine_err:
        validate_arguments({"bbox": bad}, {"type": "object", "properties": {}})
    assert str(engine_err.value) == str(common_err.value)


# ---------- geometry primitives ----------


@pytest.mark.parametrize(
    "bbox", [None, [], [0.1, 0.2], [0.1, "x", 0.9, 1.0], "garbage", {}]
)
def test_center_of_garbage_is_none(bbox) -> None:
    assert center_of(bbox) is None


def test_center_of_valid_bbox() -> None:
    assert center_of([0.2, 0.4, 0.6, 0.8]) == (0.4, 0.6000000000000001)


def test_center_of_accepts_string_floats() -> None:
    # bboxes re-transcribed by the model sometimes arrive as strings.
    assert center_of(["0.2", "0.4", "0.6", "0.8"]) is not None


@pytest.mark.parametrize("index", [0, 1, 2, 3])
@pytest.mark.parametrize(
    "value", [10**400, -(10**400), float("nan"), float("inf"), "-Infinity"]
)
def test_regression_center_of_unusable_coordinate_returns_none(index, value):
    bbox = [0.1, 0.2, 0.8, 0.9]
    bbox[index] = value

    result = center_of(bbox)

    assert result is None


@given(
    st.lists(st.floats(allow_nan=False, allow_infinity=False), min_size=4, max_size=4)
)
def test_center_of_finite_coordinates_is_finite_and_between_edges(bbox):
    result = center_of(bbox)

    assert result is not None and all(math.isfinite(coord) for coord in result)
    assert min(bbox[0], bbox[2]) <= result[0] <= max(bbox[0], bbox[2])
    assert min(bbox[1], bbox[3]) <= result[1] <= max(bbox[1], bbox[3])


def test_regression_center_of_large_finite_box_does_not_overflow():
    bbox = [1e308, -1e308, 1.5e308, -1.5e308]

    result = center_of(bbox)

    assert result == (1.25e308, -1.25e308)


def test_near_within_tolerance() -> None:
    a = (0.5, 0.5)
    assert near(a, (0.5 + 0.02 * 0.9, 0.5 - 0.02 * 0.9), tolerance=0.02)
    assert not near(a, (0.5 + 0.02 * 1.5, 0.5), tolerance=0.02)


def test_inside_zero_margin_is_exact_containment() -> None:
    box = [0.2, 0.2, 0.4, 0.4]
    assert inside((0.4, 0.3), box, margin=0.0)
    assert not inside((0.401, 0.3), box, margin=0.0)


def test_inside_expands_by_margin() -> None:
    box = [0.2, 0.2, 0.4, 0.4]
    assert inside((0.4 + 0.02, 0.3), box, margin=0.02)
    assert not inside((0.4 + 0.02 * 1.5, 0.3), box, margin=0.02)


@pytest.mark.parametrize("reader", [validate_bbox, parse_box])
@pytest.mark.parametrize("index", [0, 1, 2, 3])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_regression_bbox_rejects_nonfinite_coordinates(reader, index, value) -> None:
    box = [0.1, 0.2, 0.8, 0.9]
    box[index] = value

    with pytest.raises(ValueError, match=r"each coord must be in \[0, 1\]"):
        reader(box)


@pytest.mark.parametrize("reader", [validate_bbox, parse_box])
@pytest.mark.parametrize("box", [[False, 0.2, 0.8, 0.9], [0.1, 0.2, True, 0.9]])
def test_regression_bbox_rejects_boolean_coordinates(reader, box) -> None:
    with pytest.raises(ValueError, match="each coord must be a number"):
        reader(box)


@given(
    st.lists(st.floats(min_value=0, max_value=1), min_size=4, max_size=4).filter(
        lambda box: box[0] < box[2] and box[1] < box[3]
    )
)
def test_valid_bbox_values_survive_validation_and_parsing(box) -> None:
    validated = validate_bbox(box)
    parsed = parse_box(box)

    assert validated is box
    assert parsed == tuple(box)
