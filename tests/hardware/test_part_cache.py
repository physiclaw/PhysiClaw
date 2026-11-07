"""Leaf geometry reuse must preserve independent placement and BOM quantities."""

import pytest

pytest.importorskip("build123d")

from build123d import Box, Location

from hardware.parts import base


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
    assert second.bounding_box().center().X == pytest.approx(0)
    assert moved.bounding_box().center().X == pytest.approx(10)
    assert base.BOM_REGISTRY == [
        (("Part",), 2, "custom", None),
        (("Part",), 3, "custom", None),
    ]
