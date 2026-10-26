"""Temporary exact geometry for resuming a native HLR crash.

These checkpoints belong to one build attempt, not the persistent build
cache. The dispatcher clears them before rebuilding a stem. BREP preserves
the analytic geometry; cameras are saved from the original full assembly so
the independently loaded solid/ghost layers stay aligned.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
from pathlib import Path

from build123d import export_brep, import_brep


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(directory: Path, solid, ghost, views: dict) -> None:
    """Commit the manifest last, after both optional geometry layers land."""
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "checkpoint.json"
    manifest.unlink(missing_ok=True)
    layers = {}
    for name, shape in (("solid", solid), ("ghost", ghost)):
        if shape is None:
            continue
        path = directory / f"{name}.brep"
        tmp = path.with_suffix(".tmp")
        if not export_brep(shape, tmp):
            raise RuntimeError(f"Could not checkpoint render geometry: {path}")
        tmp.replace(path)
        layers[name] = _digest(path)
    tmp = manifest.with_suffix(".tmp")
    tmp.write_text(json.dumps({"layers": layers, "views": views}), encoding="utf-8")
    tmp.replace(manifest)


def load(directory: Path, view_indices: list[int]):
    """Return intact layers and cameras, or rebuild if a checkpoint is damaged."""
    try:
        data = json.loads((directory / "checkpoint.json").read_text(encoding="utf-8"))
        layers, views = data["layers"], data["views"]
        if (
            not isinstance(layers, dict)
            or set(layers) not in ({"solid"}, {"solid", "ghost"})
            or not isinstance(views, dict)
            or set(views) != {str(i) for i in view_indices}
        ):
            return None
        for camera in views.values():
            if len(camera) != 3 or any(
                len(vector) != 3 or not all(math.isfinite(value) for value in vector)
                for vector in camera
            ):
                return None
        for name, digest in layers.items():
            path = directory / f"{name}.brep"
            if path.is_symlink() or _digest(path) != digest:
                return None
        shapes = {name: import_brep(directory / f"{name}.brep") for name in layers}
        return shapes["solid"], shapes.get("ghost"), views
    except (OSError, ValueError, KeyError, TypeError):
        return None


def clear(directory: Path) -> None:
    if directory.exists():
        shutil.rmtree(directory)
