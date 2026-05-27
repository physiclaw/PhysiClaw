"""Validation + snapping for browser-supplied input."""

from __future__ import annotations

import re
from typing import List, Tuple

from hardware.assembly.mark.svg import Color, DEFAULT_COLOR


_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def vertex_from_click(x: float, y: float) -> Tuple[float, float]:
    """Map a raw click to a polygon vertex. Identity today; this is the
    isolated hook for future snapping (nearest edge / vertex)."""
    return (x, y)


def validate_color(raw) -> Color | None:
    """Coerce ``{fill, opacity}`` into a ``Color`` dict, or ``None`` if
    ``raw`` is missing. Raises ``ValueError`` on malformed input."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("color must be an object {fill, opacity}")
    fill = raw.get("fill")
    opacity = raw.get("opacity")
    if not isinstance(fill, str) or not _HEX_RE.match(fill):
        raise ValueError("color.fill must be a #rrggbb hex string")
    if not isinstance(opacity, (int, float)) or not (0.0 <= opacity <= 1.0):
        raise ValueError("color.opacity must be a number in [0, 1]")
    return {"fill": fill, "opacity": float(opacity)}


def validate_polygons(raw) -> List[dict]:
    """Return polygons as ``[{points, color}, ...]``.

    Each polygon carries its own ``color`` (locked at draw time in the
    UI) so swatch changes between draws produce mixed-colour ops. A
    polygon with no ``color`` falls back to ``DEFAULT_COLOR``. Drops
    incomplete polygons (< 3 vertices) silently; raises on malformed
    shapes."""
    if not isinstance(raw, list):
        raise ValueError("polygons must be a list")
    out: List[dict] = []
    for poly in raw:
        if not isinstance(poly, dict):
            raise ValueError("each polygon must be {points, color}")
        points = poly.get("points")
        if not isinstance(points, list) or len(points) < 3:
            continue
        verts: List[Tuple[float, float]] = []
        for pt in points:
            if not (isinstance(pt, (list, tuple)) and len(pt) == 2):
                raise ValueError("vertex must be [x, y]")
            verts.append(vertex_from_click(float(pt[0]), float(pt[1])))
        out.append({
            "points": verts,
            # Defensive copy so callers can mutate without aliasing the
            # shared DEFAULT_COLOR dict across polygons.
            "color":  validate_color(poly.get("color")) or {**DEFAULT_COLOR},
        })
    return out
