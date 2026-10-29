"""Exact CAD round trips and render recovery without rebuilding geometry."""

import json

import pytest

pytest.importorskip("build123d", reason="CAD group not installed")

from build123d import Box, Compound, Location

from hardware.assembly import base, build_procedures, render_checkpoint
from hardware.assembly.base import GHOST_LABEL, SOLID_LABEL, BaseAssembly
from hardware.assembly.projection import Camera

CAMERA = [[1, 2, 3], [0, 1, 0], [0, 0, 0]]


class TwoViews(BaseAssembly):
    camera = [Camera(30, 25), Camera(-40, 20)]

    @classmethod
    def _module_stem(cls):
        return "frame_10_checkpoint"

    def _build(self):
        return Compound(
            children=[
                Compound(label=SOLID_LABEL, children=[Box(3, 4, 5)]),
                Compound(
                    label=GHOST_LABEL,
                    children=[Box(2, 2, 2).move(Location((8, 0, 0)))],
                ),
            ]
        )


@pytest.fixture
def output(tmp_path, monkeypatch):
    monkeypatch.setattr(base, "RENDER_WORK_DIR", tmp_path / "checkpoints")
    monkeypatch.setattr(
        TwoViews,
        "svg_path",
        lambda self, index=None: tmp_path / f"cam{index}.svg",
    )
    monkeypatch.setattr(TwoViews, "output_path", lambda self: tmp_path / "model.step")
    return tmp_path


def test_resume_preserves_exact_layers_cameras_and_finished_views(output, monkeypatch):
    build_procedures._run_one(TwoViews, False)
    expected = [(output / f"cam{i}.svg").read_bytes() for i in range(2)]
    for i in range(2):
        (output / f"cam{i}.svg").unlink()

    project = Compound.project_to_viewport
    calls = 0

    def crash_on_second_camera(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:  # first camera completed both solid and ghost layers
            raise RuntimeError("interrupted render")
        return project(self, *args, **kwargs)

    monkeypatch.setattr(Compound, "project_to_viewport", crash_on_second_camera)
    with pytest.raises(RuntimeError, match="interrupted render"):
        TwoViews().render()
    finished = output / "cam0.svg"
    modified = finished.stat().st_mtime_ns
    assert not (output / "cam1.svg").exists()

    def cannot_build(self):
        pytest.fail("a render retry must not rebuild checkpointed geometry")

    monkeypatch.setattr(Compound, "project_to_viewport", project)
    monkeypatch.setattr(TwoViews, "_build", cannot_build)
    timing = build_procedures._run_one(TwoViews, False, only_missing=True)

    assert timing[:2] == (0.0, 0.0)
    assert finished.stat().st_mtime_ns == modified
    assert [(output / f"cam{i}.svg").read_bytes() for i in range(2)] == expected
    assert not list((output / "checkpoints").glob("*/checkpoint.json"))


def test_corrupt_checkpoint_is_rebuilt_instead_of_loaded(output):
    folder = output / "checkpoints" / "frame_10_checkpoint_assembled"
    render_checkpoint.save(folder, Box(3, 4, 5), None, {"0": CAMERA, "1": CAMERA})
    assert render_checkpoint.load(folder, [0, 1]) is not None
    (folder / "solid.brep").write_bytes(b"truncated geometry")
    TwoViews().render(only_missing=True)
    assert (output / "cam0.svg").is_file()
    assert (output / "cam1.svg").is_file()


def test_checkpoint_rejects_unexpected_layer_paths(tmp_path):
    (tmp_path / "checkpoint.json").write_text(
        json.dumps({"layers": {"../other": "abc"}, "views": {"0": CAMERA}}),
        encoding="utf-8",
    )
    assert render_checkpoint.load(tmp_path, [0]) is None


@pytest.mark.parametrize(
    "damage",
    [
        {"layers": ["solid"]},
        {"views": {}},
        {"views": {"0": [[1, 2, 3]]}},
        {"views": {"0": [[1, 2, 3], [0, 1, 0], [0, 0, float("nan")]]}},
    ],
)
def test_damaged_checkpoint_metadata_is_rejected(tmp_path, damage):
    render_checkpoint.save(tmp_path, Box(1, 2, 3), None, {"0": CAMERA})
    manifest = tmp_path / "checkpoint.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data.update(damage)
    manifest.write_text(json.dumps(data), encoding="utf-8")
    assert render_checkpoint.load(tmp_path, [0]) is None
