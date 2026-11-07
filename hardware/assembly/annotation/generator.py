"""Generate manual highlights and callouts directly from procedure models.

The build pipeline calls generate(assembly). This module owns model construction
and output files. Figure declarations live in specs; CAD rules and SVG mechanics
live in geometry, features, callouts and svg.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from hardware.assembly.projection import camera_view
from hardware.scheme import ANNOTATION_DIR, SVG_DIR

from .callouts import draw_callouts
from .features import highlight_regions
from .geometry import Projection
from .specs import figures_for
from .svg import Drawing, compose, path_svg

if TYPE_CHECKING:
    from hardware.assembly.base import BaseAssembly


def _write_figure(output: Path, result: str, intermediate: Path, paths: str) -> None:
    """Stage both files, then publish the final SVG as the completion marker."""
    files = ((intermediate, paths), (output, result))
    try:
        for destination, contents in files:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.with_suffix(".tmp").write_text(contents, encoding="utf-8")
        # A failed commit must leave the final missing so only_missing retries.
        output.unlink(missing_ok=True)
        for destination, _ in files:
            destination.with_suffix(".tmp").replace(destination)
    finally:
        for destination, _ in files:
            destination.with_suffix(".tmp").unlink(missing_ok=True)


def generate(assembly: BaseAssembly, *, only_missing: bool = False) -> list[Path]:
    """Write path intermediates and composed figures for one procedure variant.

    Input: a procedure instance whose raw SVG views have already been rendered.
    Output: path-only intermediates and final figures; return the final paths
    written. CAD is built only when a declared figure needs output. Both files
    must exist for only_missing to skip it; the raw drawing is never modified.
    """
    pending_figures = []
    for filename, figure in figures_for(
        assembly._module_stem(), assembly.exploded
    ).items():
        output = SVG_DIR / filename
        intermediate = ANNOTATION_DIR / f"{output.stem}.paths.svg"
        if only_missing and output.is_file() and intermediate.is_file():
            continue
        pending_figures.append((figure, output, intermediate))
    if not pending_figures:
        return []

    visible_root = assembly.build()
    assembled_root = None
    written = []
    for figure, output, intermediate in pending_figures:
        camera_index = int(figure.view.rsplit("cam", 1)[1])
        projection = Projection(
            camera_view(visible_root, assembly.cameras[camera_index])
        )
        if (
            assembled_root is None
            and assembly.exploded
            and any(h.assembled for h in figure.highlights)
        ):
            assembled_root = type(assembly)(exploded=False).build()

        drawing = Drawing()
        for highlight, region in highlight_regions(
            figure.highlights,
            visible_root=visible_root,
            assembled_root=visible_root if assembled_root is None else assembled_root,
            projection=projection,
        ):
            drawing.region(region, highlight.color, highlight.opacity)
        if figure.callout:
            draw_callouts(
                figure.callout,
                root=visible_root,
                projection=projection,
                drawing=drawing,
            )

        original = (SVG_DIR / f"{figure.view}.svg").read_text(encoding="utf-8")
        paths = path_svg(original, drawing, figure.crop)
        result = compose(original, paths)
        _write_figure(output, result, intermediate, paths)
        written.append(output)
    return written
