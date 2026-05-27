"""Build the snapshot SVG: original line art + polygon overlay + optional
cropped viewBox. The original file is read once as bytes; the output is
returned as bytes so the caller controls where it lands on disk."""

from __future__ import annotations

import re
from typing import Iterable, List, TypedDict

from hardware.assembly.svg_utils import set_root_viewbox

FILL_GROUP_ID = "manual-fill"


class Color(TypedDict):
    """Polygon style — one of the six palette presets in the UI."""
    fill:    str   # e.g. "#1e88ff"
    opacity: float # 0.0 .. 1.0


# Default applied to polygons missing a ``color`` field (legacy entries
# + the UI's initial swatch selection).
DEFAULT_COLOR: Color = {"fill": "#1e88ff", "opacity": 0.35}

# Last ``</svg>`` closer in the file — polygons are inserted just before
# it so a file with a trailing comment or nested ``</svg>`` inside CDATA
# can't trick us into inserting too early.
_SVG_CLOSE_RE = re.compile(r"</\s*svg\s*>", re.IGNORECASE)


def build_fill_svg(
    original: bytes,
    polygons: Iterable[dict],
    viewbox: str | None = None,
) -> bytes:
    """Return the original SVG bytes with a single
    ``<g id='manual-fill'>`` (containing each polygon) appended just
    before the closing ``</svg>``. ``viewbox`` rewrites the root
    ``<svg>``'s ``viewBox`` when given. Each polygon is a dict
    ``{points: [(x, y), ...], color: {fill, opacity}}``; ``color``
    falls back to ``DEFAULT_COLOR`` if missing. Everything else is
    byte-identical."""
    text = original.decode("utf-8")
    matches = list(_SVG_CLOSE_RE.finditer(text))
    if not matches:
        raise ValueError("input does not contain </svg>")
    insert_at = matches[-1].start()

    # Group polygons by colour into nested ``<g fill=... fill-opacity=...>``
    # so each <polygon> doesn't re-declare the same attrs — palette has
    # only six colours, so polygons collide on (fill, opacity) often.
    groups: dict[tuple[str, float], list[str]] = {}
    for poly in polygons:
        c = poly.get("color") or DEFAULT_COLOR
        pts = " ".join(f"{x:.4f},{y:.4f}" for x, y in poly["points"])
        groups.setdefault((c["fill"], c["opacity"]), []).append(pts)

    lines: List[str] = [
        "  <!-- manual-fill: appended by hardware/assembly/mark -->",
        f'  <g id="{FILL_GROUP_ID}" class="{FILL_GROUP_ID}" stroke="none">',
    ]
    for (fill, opacity), pts_list in groups.items():
        lines.append(f'    <g fill="{fill}" fill-opacity="{opacity}">')
        for pts in pts_list:
            lines.append(f'      <polygon points="{pts}"/>')
        lines.append("    </g>")
    lines.append("  </g>")
    block = "\n" + "\n".join(lines) + "\n"

    text = text[:insert_at] + block + text[insert_at:]
    if viewbox is not None:
        text = set_root_viewbox(text, viewbox)
    return text.encode("utf-8")
