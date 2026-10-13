"""Tests for `physiclaw.core.calibration.state` — the Calibration bundle.

The bundle is a dataclass with optional fields and JSON persistence.
Tests cover:

  - ROTATION_NAMES + DEFAULT_ROTATION constants
  - empty/partial/full state via transforms_ready, complete,
    transforms(), pct_to_grbl_mm, summary(), effective_rotation()
  - to_dict/from_dict full + empty round-trips, including numpy
    array preservation
  - save/load disk path: parent-dir creation, missing-file → None,
    malformed-json → None, round-trip via save→load

`BUNDLE_PATH` is captured at import time and isn't tested via the
default; tests pass an explicit `path=` to save/load.

Accepted equivalent mutants: dataclass field annotations of the shape
`Type | None` mutated to `Type & None`. The module has
`from __future__ import annotations`, so annotations are strings —
never evaluated at runtime. The dataclass machinery treats them as
opaque strings, and Python doesn't care about the operator.
"""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from physiclaw.core.calibration.state import (
    DEFAULT_ROTATION,
    ROTATION_NAMES,
    Calibration,
)
from physiclaw.core.calibration.transforms import ScreenTransforms, ViewportShift

# ---------- constants ----------


def test_rotation_names_maps_each_cv2_code() -> None:
    assert ROTATION_NAMES == {
        -1: "none",
        cv2.ROTATE_90_CLOCKWISE: "90° CW",
        cv2.ROTATE_180: "180°",
        cv2.ROTATE_90_COUNTERCLOCKWISE: "90° CCW",
    }


def test_default_rotation_is_counterclockwise() -> None:
    assert DEFAULT_ROTATION == cv2.ROTATE_90_COUNTERCLOCKWISE


def test_bundle_path_is_set_to_calibration_bundle_at_import() -> None:
    from physiclaw.core.calibration import state as state_mod

    assert state_mod.BUNDLE_PATH is not None
    assert state_mod.BUNDLE_PATH.name == "bundle.json"


# ---------- empty / partial / full state ----------


def test_empty_calibration_has_all_fields_none() -> None:
    c = Calibration()

    assert c.viewport_shift is None
    assert c.cam_rotation is None
    assert c.pct_to_grbl is None
    assert c.pct_to_cam is None
    assert c.cam_size is None
    assert c.cam_index is None
    assert c.screen_dimension is None


def test_transforms_ready_false_when_any_mapping_field_missing() -> None:
    affine = np.eye(2, 3)
    cam = (1920, 1080)

    # All three required: pct_to_grbl, pct_to_cam, cam_size.
    assert Calibration(pct_to_grbl=affine, pct_to_cam=affine).transforms_ready is False
    assert Calibration(pct_to_grbl=affine, cam_size=cam).transforms_ready is False
    assert Calibration(pct_to_cam=affine, cam_size=cam).transforms_ready is False


def test_transforms_ready_true_when_all_three_mapping_fields_set() -> None:
    affine = np.eye(2, 3)
    c = Calibration(pct_to_grbl=affine, pct_to_cam=affine, cam_size=(1920, 1080))

    assert c.transforms_ready is True


def test_complete_property_is_false_until_every_required_field_set() -> None:
    affine = np.eye(2, 3)
    vs = ViewportShift(
        offset_x=0,
        offset_y=0,
        dpr=1.0,
        screenshot_width=100,
        screenshot_height=200,
    )

    # transforms_ready alone isn't enough — need viewport_shift,
    # cam_rotation, screen_dimension on top.
    c = Calibration(
        pct_to_grbl=affine,
        pct_to_cam=affine,
        cam_size=(1920, 1080),
    )
    assert c.complete is False

    c.viewport_shift = vs
    c.cam_rotation = cv2.ROTATE_90_CLOCKWISE
    assert c.complete is False  # still missing screen_dimension

    c.screen_dimension = {
        "width": 1170,
        "height": 2532,
        "viewport_width": 390,
        "viewport_height": 700,
    }
    assert c.complete is True


# ---------- transforms() factory ----------


def test_transforms_returns_none_when_not_ready() -> None:
    assert Calibration().transforms() is None


def test_transforms_returns_screen_transforms_with_passed_through_fields() -> None:
    pct_to_grbl = np.array([[100.0, 0.0, 5.0], [0.0, 200.0, 10.0]])
    pct_to_cam = np.eye(2, 3)
    c = Calibration(
        pct_to_grbl=pct_to_grbl,
        pct_to_cam=pct_to_cam,
        cam_size=(800, 600),
    )

    t = c.transforms()

    assert isinstance(t, ScreenTransforms)
    assert np.array_equal(t.pct_to_grbl, pct_to_grbl)
    assert np.array_equal(t.pct_to_cam, pct_to_cam)
    assert t.cam_size == (800, 600)


# ---------- pct_to_grbl_mm ----------


def test_pct_to_grbl_mm_returns_none_when_affine_unset() -> None:
    assert Calibration().pct_to_grbl_mm(0.5, 0.5) is None


def test_pct_to_grbl_mm_applies_affine_directly_without_camera_mapping() -> None:
    # x scaled ×100 + offset 5; y scaled ×200 + offset 10.
    pct_to_grbl = np.array([[100.0, 0.0, 5.0], [0.0, 200.0, 10.0]])
    c = Calibration(pct_to_grbl=pct_to_grbl)

    x_mm, y_mm = c.pct_to_grbl_mm(0.5, 0.25)

    assert (x_mm, y_mm) == pytest.approx((55.0, 60.0))


# ---------- summary() ----------


def test_summary_returns_empty_dict_for_empty_calibration() -> None:
    assert Calibration().summary() == {}


def test_summary_includes_filled_fields_with_per_field_formatting() -> None:
    vs = ViewportShift(
        offset_x=10,
        offset_y=20,
        dpr=2.0,
        screenshot_width=200,
        screenshot_height=400,
    )
    c = Calibration(
        viewport_shift=vs,
        cam_rotation=cv2.ROTATE_180,
        pct_to_grbl=np.eye(2, 3),
        pct_to_cam=np.eye(2, 3),
        cam_size=(1920, 1080),
    )

    summary = c.summary()

    assert summary["viewport_shift"] == "dpr=2.0, offset=(10, 20)"
    assert summary["rotation"] == "180°"
    assert summary["mapping_a"] == "OK"
    assert summary["mapping_b"] == "OK"
    assert summary["validated"] is True


def test_summary_uses_string_fallback_for_unknown_rotation_code() -> None:
    # ROTATION_NAMES has codes -1, 0, 1, 2; an unmapped code falls back
    # to its string repr.
    c = Calibration(cam_rotation=99)

    assert c.summary()["rotation"] == "99"


def test_summary_omits_validated_key_when_only_one_mapping_is_set() -> None:
    c = Calibration(pct_to_grbl=np.eye(2, 3))

    summary = c.summary()

    assert "mapping_a" in summary
    assert "validated" not in summary


# ---------- effective_rotation ----------


def test_effective_rotation_returns_set_value() -> None:
    c = Calibration(cam_rotation=cv2.ROTATE_180)

    assert c.effective_rotation() == cv2.ROTATE_180


def test_effective_rotation_falls_back_to_default_when_unset() -> None:
    assert Calibration().effective_rotation() == DEFAULT_ROTATION


# ---------- to_dict / from_dict ----------


def test_to_dict_with_empty_calibration_yields_all_nones() -> None:
    payload = Calibration().to_dict()

    assert payload == {
        "version": 1,
        "viewport_shift": None,
        "cam_rotation": None,
        "pct_to_grbl": None,
        "pct_to_cam": None,
        "cam_size": None,
        "cam_index": None,
        "screen_dimension": None,
        "cam_focus": None,
    }


def test_to_dict_serializes_numpy_arrays_and_tuple_cam_size() -> None:
    pct_to_grbl = np.array([[100.0, 0.0, 5.0], [0.0, 200.0, 10.0]])
    c = Calibration(
        pct_to_grbl=pct_to_grbl,
        cam_size=(800, 600),
    )

    payload = c.to_dict()

    assert payload["pct_to_grbl"] == [[100.0, 0.0, 5.0], [0.0, 200.0, 10.0]]
    assert payload["cam_size"] == [800, 600]  # tuple → list


def test_from_dict_round_trips_a_full_bundle() -> None:
    pct_to_grbl = np.array([[100.0, 0.0, 5.0], [0.0, 200.0, 10.0]])
    pct_to_cam = np.eye(2, 3)
    vs = ViewportShift(
        offset_x=10,
        offset_y=20,
        dpr=2.0,
        screenshot_width=200,
        screenshot_height=400,
    )
    original = Calibration(
        viewport_shift=vs,
        cam_rotation=cv2.ROTATE_90_CLOCKWISE,
        pct_to_grbl=pct_to_grbl,
        pct_to_cam=pct_to_cam,
        cam_size=(800, 600),
        cam_index=1,
        screen_dimension={"width": 1170, "height": 2532},
        cam_focus=137.0,
    )

    restored = Calibration.from_dict(original.to_dict())

    assert restored.viewport_shift == vs
    assert restored.cam_rotation == cv2.ROTATE_90_CLOCKWISE
    assert np.array_equal(restored.pct_to_grbl, pct_to_grbl)
    assert np.array_equal(restored.pct_to_cam, pct_to_cam)
    assert restored.cam_size == (800, 600)
    assert restored.cam_index == 1
    assert restored.screen_dimension == {"width": 1170, "height": 2532}
    assert restored.cam_focus == 137.0


def test_from_dict_without_cam_focus_key_loads_as_none() -> None:
    # Pre-cam_focus bundles keep loading (alpha: no forced
    # recalibration) — the lens just stays on live AF.
    restored = Calibration.from_dict({"version": 1})

    assert restored.cam_focus is None


def test_from_dict_with_all_nones_returns_empty_calibration() -> None:
    restored = Calibration.from_dict(
        {
            "viewport_shift": None,
            "cam_rotation": None,
            "pct_to_grbl": None,
            "pct_to_cam": None,
            "cam_size": None,
            "cam_index": None,
            "screen_dimension": None,
        }
    )

    assert restored == Calibration()


def test_from_dict_rejects_schema_version_mismatch() -> None:
    """A bundle from an incompatible format must fail loudly, not
    reconstruct with silently-wrong fields."""
    import pytest

    with pytest.raises(ValueError, match="schema version 99"):
        Calibration.from_dict({"version": 99})


def test_roundtrip_carries_schema_version() -> None:
    payload = Calibration().to_dict()

    assert payload["version"] == 1
    assert Calibration.from_dict(payload) == Calibration()


def test_load_returns_none_on_version_mismatch(tmp_path) -> None:
    import json

    p = tmp_path / "bundle.json"
    p.write_text(json.dumps({"version": 99}))

    assert Calibration.load(p) is None


def test_from_dict_ignores_unknown_keys() -> None:
    # from_dict reads only known fields, so a bundle carrying extra keys (an
    # older or newer format) loads without error and ignores them.
    restored = Calibration.from_dict({"cam_index": 1, "some_future_field": 42})

    assert restored.cam_index == 1
    assert not hasattr(restored, "some_future_field")


def test_from_dict_reconstructs_pct_to_grbl_as_float64_ndarray() -> None:
    payload = {
        "viewport_shift": None,
        "cam_rotation": None,
        "pct_to_grbl": [[1, 0, 0], [0, 1, 0]],
        "pct_to_cam": None,
        "cam_size": None,
        "cam_index": None,
        "screen_dimension": None,
    }

    restored = Calibration.from_dict(payload)

    assert restored.pct_to_grbl.dtype == np.float64
    assert restored.pct_to_grbl.shape == (2, 3)


# ---------- save / load ----------


def test_save_writes_two_space_indented_json_to_disk(tmp_path: Path) -> None:
    bundle_path = tmp_path / "bundle.json"
    c = Calibration(cam_index=1, cam_size=(800, 600))

    c.save(bundle_path)

    assert bundle_path.is_file()
    text = bundle_path.read_text()
    # Byte-for-byte equality with write_json_atomic's rendering — kills
    # any mutation on the indent argument, and pins the atomic writer as
    # the single serialization path.
    assert text == json.dumps(c.to_dict(), ensure_ascii=False, indent=2) + "\n"
    # tmp + rename: no .json.tmp residue next to the bundle.
    assert not list(bundle_path.parent.glob("*.tmp"))


def test_save_creates_intermediate_parent_directories(tmp_path: Path) -> None:
    deep = tmp_path / "a" / "b" / "c" / "bundle.json"

    Calibration().save(deep)

    assert deep.is_file()


def test_load_returns_none_when_file_missing(tmp_path: Path) -> None:
    assert Calibration.load(tmp_path / "missing.json") is None


def test_load_returns_none_on_malformed_json(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("{not valid json")

    assert Calibration.load(p) is None


def test_load_returns_none_on_payload_missing_required_keys(tmp_path: Path) -> None:
    # ViewportShift(**vs) raises TypeError if vs is missing required
    # kwargs — load must catch it and return None.
    p = tmp_path / "partial.json"
    p.write_text(json.dumps({"viewport_shift": {"offset_x": 0}}))

    assert Calibration.load(p) is None


def test_load_logs_warning_on_corrupt_bundle(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    p = tmp_path / "bad.json"
    p.write_text("{not valid json")

    with caplog.at_level(logging.WARNING, logger="physiclaw.core.calibration.state"):
        Calibration.load(p)

    assert any(
        r.getMessage().startswith("Failed to load calibration bundle from")
        for r in caplog.records
    )


def test_save_then_load_round_trips_to_an_equal_bundle(tmp_path: Path) -> None:
    bundle_path = tmp_path / "bundle.json"
    pct_to_grbl = np.array([[100.0, 0.0, 5.0], [0.0, 200.0, 10.0]])
    original = Calibration(
        cam_rotation=cv2.ROTATE_180,
        pct_to_grbl=pct_to_grbl,
        cam_size=(800, 600),
    )

    original.save(bundle_path)
    restored = Calibration.load(bundle_path)

    assert restored is not None
    assert restored.cam_rotation == original.cam_rotation
    assert np.array_equal(restored.pct_to_grbl, pct_to_grbl)
    assert restored.cam_size == original.cam_size


def test_save_logs_info_message_with_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    bundle_path = tmp_path / "bundle.json"

    with caplog.at_level(logging.INFO, logger="physiclaw.core.calibration.state"):
        Calibration().save(bundle_path)

    assert any(
        r.getMessage() == f"Saved calibration bundle → {bundle_path}"
        for r in caplog.records
    )


# ---------- reconcile_cam_size ----------


def test_reconcile_cam_size_true_when_sizes_equal() -> None:
    cal = Calibration(cam_size=(1920, 1080))

    assert cal.reconcile_cam_size((1920, 1080)) is True
    assert cal.cam_size == (1920, 1080)


@pytest.mark.parametrize(
    "live_size",
    [(3840, 2160), (2560, 1440)],  # 4K and 2K, both 16:9 like the calibrated size
)
def test_reconcile_cam_size_adopts_live_size_on_same_aspect(
    live_size: tuple[int, int],
) -> None:
    cal = Calibration(cam_size=(1920, 1080))

    assert cal.reconcile_cam_size(live_size) is True
    assert cal.cam_size == live_size


def test_reconcile_cam_size_rejects_aspect_change() -> None:
    cal = Calibration(cam_size=(1920, 1080))

    assert cal.reconcile_cam_size((1920, 1440)) is False
    assert cal.cam_size == (1920, 1080)


def test_reconcile_cam_size_true_when_unset() -> None:
    cal = Calibration()

    assert cal.reconcile_cam_size((1920, 1080)) is True
    assert cal.cam_size is None


def test_reconcile_cam_size_handles_rotated_portrait_sizes() -> None:
    # cam_size lives in ROTATED frame space; a portrait bundle scales the
    # same way.
    cal = Calibration(cam_size=(1080, 1920))

    assert cal.reconcile_cam_size((2160, 3840)) is True
    assert cal.cam_size == (2160, 3840)


@pytest.mark.parametrize("payload", [[], [1], "bundle", 1, True, None])
def test_regression_load_rejects_non_object_json(tmp_path, payload) -> None:
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(payload))

    result = Calibration.load(path)

    assert result is None


@pytest.mark.parametrize("field", ["pct_to_grbl", "pct_to_cam"])
@pytest.mark.parametrize(
    "value",
    [
        [],
        [[1, 0], [0, 1]],
        [[1, 0, 0], [0]],
        "bad",
        [[1, 1, 0], [2, 2, 1]],
        [[1, 0, float("nan")], [0, 1, 0]],
        [[float("inf"), 0, 0], [0, 1, 0]],
        [[10**400, 0, 0], [0, 1, 0]],
    ],
)
def test_regression_from_dict_rejects_unusable_affines(field, value) -> None:
    payload = {field: value}

    with pytest.raises(ValueError, match=f"invalid {field}"):
        Calibration.from_dict(payload)


@pytest.mark.parametrize(
    "value", [[], [1], [1, 2, 3], "12", {}, [0, 1], [-1, 1], [1.5, 2], [True, 2]]
)
def test_regression_from_dict_rejects_invalid_camera_dimensions(value) -> None:
    payload = {"cam_size": value}

    with pytest.raises(ValueError, match="cam_size"):
        Calibration.from_dict(payload)


@pytest.mark.parametrize("value", [True, 1.0, "1", None])
def test_from_dict_rejects_non_integer_schema_versions(value) -> None:
    payload = {"version": value}

    with pytest.raises(ValueError, match="schema version"):
        Calibration.from_dict(payload)


@pytest.mark.parametrize("value", [True, 1.0, "1", -2, 3, [], {}])
def test_from_dict_rejects_invalid_rotation_codes(value) -> None:
    payload = {"cam_rotation": value}

    with pytest.raises(ValueError, match="cam_rotation"):
        Calibration.from_dict(payload)


@pytest.mark.parametrize("value", [True, 1.0, "1", -1])
def test_from_dict_rejects_invalid_camera_indices(value) -> None:
    payload = {"cam_index": value}

    with pytest.raises(ValueError, match="cam_index"):
        Calibration.from_dict(payload)


@pytest.mark.parametrize("value", [True, "1", -1, float("nan"), float("inf"), 10**400])
def test_from_dict_rejects_invalid_focus_values(value) -> None:
    payload = {"cam_focus": value}

    with pytest.raises(ValueError, match="cam_focus"):
        Calibration.from_dict(payload)


@pytest.mark.parametrize("value", [[], {"offset_x": 0}])
def test_from_dict_rejects_malformed_viewport_shift(value) -> None:
    payload = {"viewport_shift": value}

    with pytest.raises(ValueError, match="viewport_shift"):
        Calibration.from_dict(payload)


@pytest.mark.parametrize(
    "field, value",
    [
        ("offset_x", float("nan")),
        ("offset_y", "0"),
        ("dpr", 0),
        ("dpr", -1),
        ("dpr", True),
        ("screenshot_width", 0),
        ("screenshot_height", 1.5),
    ],
)
def test_from_dict_rejects_unusable_viewport_shift(field, value) -> None:
    shift = dict(
        offset_x=0, offset_y=0, dpr=2, screenshot_width=100, screenshot_height=200
    )
    shift[field] = value

    with pytest.raises(ValueError, match="viewport_shift"):
        Calibration.from_dict({"viewport_shift": shift})


@pytest.mark.parametrize("value", [[], 0, "100x200"])
def test_from_dict_rejects_non_object_screen_dimension(value) -> None:
    payload = {"screen_dimension": value}

    with pytest.raises(ValueError, match="screen_dimension"):
        Calibration.from_dict(payload)


@pytest.mark.parametrize(
    "field", ["width", "height", "viewport_width", "viewport_height", "dpr"]
)
@pytest.mark.parametrize("value", [0, -1, True, "100", float("nan"), float("inf")])
def test_from_dict_rejects_unusable_screen_dimensions(field, value) -> None:
    payload = {"screen_dimension": {field: value}}

    with pytest.raises(ValueError, match=f"screen_dimension.{field}"):
        Calibration.from_dict(payload)


def test_load_returns_none_when_bundle_path_is_unreadable(tmp_path) -> None:
    path = tmp_path / "bundle.json"
    path.mkdir()

    result = Calibration.load(path)

    assert result is None


def test_from_dict_preserves_offscreen_offsets_and_zero_focus() -> None:
    original = Calibration(
        viewport_shift=ViewportShift(-10, -20, 2, 100, 200),
        pct_to_grbl=np.array([[-100, 0, -5], [0, 200, 10]]),
        cam_index=0,
        cam_focus=0,
        cam_rotation=-1,
        screen_dimension={"width": 100, "height": 200, "future": "value"},
    )

    restored = Calibration.from_dict(original.to_dict())

    assert restored.to_dict() == original.to_dict()
    assert restored.pct_to_grbl_mm(-0.1, -0.05) == pytest.approx((5, 0))


@given(
    x_scale=st.floats(0.01, 1000),
    y_scale=st.floats(-1000, -0.01),
    offset=st.tuples(st.floats(-1000, 1000), st.floats(-1000, 1000)),
    cam_size=st.tuples(st.integers(1, 4000), st.integers(1, 4000)),
)
def test_from_dict_preserves_valid_partial_bundle_through_json(
    x_scale, y_scale, offset, cam_size
) -> None:
    original = Calibration(
        pct_to_grbl=np.array([[x_scale, 0, offset[0]], [0, y_scale, offset[1]]]),
        cam_size=cam_size,
    )

    restored = Calibration.from_dict(json.loads(json.dumps(original.to_dict())))

    assert restored.to_dict() == original.to_dict()
    assert restored.complete is False


@pytest.mark.parametrize(
    "dimension",
    [
        {},
        {"width": 100, "height": 200},
        {"viewport_width": 100},
        {"viewport_height": 200},
    ],
)
def test_regression_partial_screen_dimensions_load_but_cannot_resume(dimension) -> None:
    original = Calibration(
        viewport_shift=ViewportShift(0, 0, 1, 100, 200),
        cam_rotation=-1,
        pct_to_grbl=np.eye(2, 3),
        pct_to_cam=np.eye(2, 3),
        cam_size=(100, 200),
        screen_dimension=dimension,
    )

    restored = Calibration.from_dict(original.to_dict())

    assert restored.to_dict() == original.to_dict()
    assert restored.complete is False


@pytest.mark.parametrize("field", ["cam_size", "viewport_shift"])
def test_regression_load_rejects_dimensions_that_overflow_coordinate_math(
    tmp_path, field
) -> None:
    values = {
        "cam_size": [10**400, 1],
        "viewport_shift": dict(
            offset_x=0, offset_y=0, dpr=1, screenshot_width=1, screenshot_height=10**400
        ),
    }
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps({field: values[field]}))

    result = Calibration.load(path)

    assert result is None


@pytest.mark.parametrize("field", ["pct_to_grbl", "pct_to_cam"])
def test_regression_from_dict_rejects_affine_with_unusable_inverse(field) -> None:
    payload = {field: [[1e-320, 0, 0], [0, 1e-320, 0]]}

    with pytest.raises(ValueError, match="inverse must be finite"):
        Calibration.from_dict(payload)
