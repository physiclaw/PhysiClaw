"""Build the snapshot SVG: original line art + polygon overlay + optional
cropped viewBox. The original file is read once as bytes; the output is
returned as bytes so the caller controls where it lands on disk."""

from __future__ import annotations

import re
from typing import Iterable, List, Tuple

from hardware.assembly.svg_utils import set_root_viewbox

FILL_GROUP_ID       = "manual-fill"
POLY_FILL           = "#1e88ff"
POLY_FILL_OPACITY   = "0.35"
POLY_STROKE         = "#1e88ff"
POLY_STROKE_OPACITY = "0.35"

# Last ``</svg>`` closer in the file — polygons are inserted just before
# it so a file with a trailing comment or nested ``</svg>`` inside CDATA
# can't trick us into inserting too early.
_SVG_CLOSE_RE = re.compile(r"</\s*svg\s*>", re.IGNORECASE)


def build_fill_svg(
    original: bytes,
    polygons: Iterable[Iterable[Tuple[float, float]]],
    viewbox: str | None = None,
) -> bytes:
    """Return the original SVG bytes with a single
    ``<g id='manual-fill'>`` (containing each polygon) appended just
    before the closing ``</svg>``. If ``viewbox`` is given, the root
    ``<svg>``'s ``viewBox`` is rewritten so the saved file shows the
    cropped region. Everything else is byte-identical."""
    text = original.decode("utf-8")
    matches = list(_SVG_CLOSE_RE.finditer(text))
    if not matches:
        raise ValueError("input does not contain </svg>")
    insert_at = matches[-1].start()

    lines: List[str] = [
        "  <!-- manual-fill: appended by hardware/assembly/mark -->",
        f'  <g id="{FILL_GROUP_ID}" class="{FILL_GROUP_ID}">',
    ]
    for poly in polygons:
        pts = " ".join(f"{x:.4f},{y:.4f}" for x, y in poly)
        lines.append(
            f'    <polygon class="{FILL_GROUP_ID}" points="{pts}" '
            f'fill="{POLY_FILL}" fill-opacity="{POLY_FILL_OPACITY}" '
            f'stroke="none"/>'
        )
    lines.append("  </g>")
    block = "\n" + "\n".join(lines) + "\n"

    text = text[:insert_at] + block + text[insert_at:]
    if viewbox is not None:
        text = set_root_viewbox(text, viewbox)
    return text.encode("utf-8")
