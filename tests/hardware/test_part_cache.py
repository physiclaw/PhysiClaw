"""Leaf geometry reuse must preserve independent placement and BOM quantities."""

import pytest

pytest.importorskip("build123d")

from build123d import Box, Location

from hardware.parts import base
from hardware.parts.standard.extrusion import (
    Extrusion1020,
    Extrusion2020,
    Extrusion2040,
)
from hardware.parts.standard.motor import Nema17Motor


@pytest.mark.slow
@pytest.mark.parametrize(
    ("part_type", "options"),
    [
        (Nema17Motor, {}),
        (Extrusion2020, {}),
        (Extrusion2040, {}),
        (Extrusion2040, {"cb": True}),
        (Extrusion1020, {}),
        (Extrusion1020, {"hole": True}),
    ],
)
def test_fractional_dimensions_do_not_reuse_whole_millimeter_geometry(
    monkeypatch, part_type, options
):
    monkeypatch.setattr(base, "_BUILD_CACHE", {})
    monkeypatch.setattr(base, "BOM_REGISTRY", [])
    shorter = part_type(100.1, **options)
    longer = part_type(100.9, **options)

    # Purchasing labels and export names retain their existing nominal sizes.
    assert shorter.bom_key() == longer.bom_key()
    assert shorter.output_path() == longer.output_path()
    short_height = shorter.build().bounding_box().size.Z
    long_height = longer.build().bounding_box().size.Z
    assert long_height - short_height == pytest.approx(0.8)


def test_cached_leaves_have_independent_placement_labels_and_bom_entries(monkeypatch):
    monkeypatch.setattr(base, "_BUILD_CACHE", {})
    monkeypatch.setattr(base, "BOM_REGISTRY", [])
    builds = []

    class Part(base.BaseCustomPart):
        def _build(self):
            builds.append(self)
            return Box(1, 2, 3)

    first = Part(qty=2)
    moved = first.build()
    moved.move(Location((10, 0, 0)))
    moved.label = "placed part"
    second = Part(qty=3).build()

    assert builds == [first]
    assert first.build() is moved
    assert second is not moved
    assert second.label == "Part"
    assert pytest.approx(0) == second.bounding_box().center().X
    assert pytest.approx(10) == moved.bounding_box().center().X
    assert base.BOM_REGISTRY == [
        (("Part",), 2, "custom", None),
        (("Part",), 3, "custom", None),
    ]


@pytest.mark.parametrize("raises", [False, True])
def test_failed_step_export_preserves_previous_output_and_removes_partial_file(
    tmp_path, monkeypatch, raises
):
    tmp_path = tmp_path / "exports"
    tmp_path.mkdir()
    target = tmp_path / "part.step"
    target.write_bytes(b"previous complete STEP")
    part = base.BasePart()
    monkeypatch.setattr(part, "build", lambda: object())
    monkeypatch.setattr(part, "output_path", lambda: target)

    def fail(shape, path):
        from pathlib import Path

        Path(path).write_bytes(b"partial export")
        if raises:
            raise RuntimeError("export failed")
        return False

    monkeypatch.setattr(base, "export_step", fail)
    with pytest.raises(RuntimeError):
        part.export()
    assert target.read_bytes() == b"previous complete STEP"
    assert list(tmp_path.iterdir()) == [target]
