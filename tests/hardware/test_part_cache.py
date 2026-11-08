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
