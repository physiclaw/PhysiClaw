"""Browser-driven polygon / crop tool for assembly-step SVGs.

Run:

    uv run --group cad python -m hardware.assembly.mark <input.svg>

Each save produces ``<src stem>.<id>.svg`` next to the source and
appends an op entry to ``hardware/assembly/patch/<src stem>.json``. An
op is ``{id, preop, shapes, viewBox}`` — shapes are typed
(polygon / rect / circle / ellipse / line / arrow), each with its
own colour + outlined flag. ``preop`` is the literal ``"orig"``
(apply on the source) or another op's 4-letter id (apply on that
op's output), so a replay script can chain ops and reproduce every
intermediate snapshot from a freshly-built original.

Modules:

* :mod:`hardware.assembly.mark.svg`      — build the snapshot SVG.
* :mod:`hardware.assembly.mark.patch`    — id naming + JSON accumulator.
* :mod:`hardware.assembly.mark.validate` — input validation / snapping.
* :mod:`hardware.assembly.mark.server`   — HTTP UI server.
* :mod:`hardware.assembly.mark.__main__` — CLI entry point.
"""

from hardware.assembly.mark.patch import (
    load_patch,
    make_entry,
    patch_path,
    write_patch,
)
from hardware.assembly.mark.svg import build_shapes_svg
from hardware.assembly.mark.validate import validate_shapes

__all__ = [
    "build_shapes_svg",
    "load_patch",
    "make_entry",
    "patch_path",
    "validate_shapes",
    "write_patch",
]
