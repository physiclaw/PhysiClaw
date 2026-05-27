"""Validation + snapping for browser-supplied input."""

from __future__ import annotations

from typing import List, Tuple


def vertex_from_click(x: float, y: float) -> Tuple[float, float]:
    """Map a raw click to a polygon vertex. Identity today; this is the
    isolated hook for future snapping (nearest edge / vertex)."""
    return (x, y)


def validate_polygons(raw) -> List[List[Tuple[float, float]]]:
    """Return the polygons coerced to ``[[(x, y), ...], ...]``. Drops
    incomplete polygons (< 3 vertices) silently; raises ``ValueError``
    on a non-list payload or a malformed vertex."""
    out: List[List[Tuple[float, float]]] = []
    if not isinstance(raw, list):
        raise ValueError("polygons must be a list")
    for poly in raw:
        if not isinstance(poly, list) or len(poly) < 3:
            continue
        verts: List[Tuple[float, float]] = []
        for pt in poly:
            if not (isinstance(pt, (list, tuple)) and len(pt) == 2):
                raise ValueError("vertex must be [x, y]")
            verts.append(vertex_from_click(float(pt[0]), float(pt[1])))
        out.append(verts)
    return out
