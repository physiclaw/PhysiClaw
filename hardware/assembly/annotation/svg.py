"""Projected 2D shapes to path SVGs; no CAD models or filesystem access.

Drawing records filled regions, lines and arrows in projected millimetres.
path_svg fits their frame; compose layers them over the untouched CAD line art.
Line widths use non-scaling strokes so they stay legible across detail crops.
"""

from __future__ import annotations

import math
from xml.etree import ElementTree as ET

from shapely.geometry import Polygon

from hardware.assembly.annotation.specs import BLUE
from hardware.assembly.svg_utils import get_root_viewbox, set_root_viewbox


def _path(region) -> str:
    polygons = list(region.geoms) if hasattr(region, "geoms") else [region]
    paths = []
    for poly in polygons:
        if not isinstance(poly, Polygon) or poly.is_empty:
            continue
        for ring in (poly.exterior, *poly.interiors):
            paths.append(
                "M " + " L ".join(f"{x:.3f},{y:.3f}" for x, y in ring.coords) + " Z"
            )
    return " ".join(paths)


class Drawing:
    """Collect paths and bounds from projected 2D geometry; all data stays in memory."""

    def __init__(self):
        self.elements = []
        self.bounds = []

    def region(self, region, color=BLUE, opacity=0.35):
        if region.is_empty:
            raise ValueError("Highlighted part has no projected area")
        self.elements.append(
            f'<path d="{_path(region)}" fill="{color}" fill-opacity="{opacity}" '
            'fill-rule="evenodd" stroke="none"/>'
        )
        self.bounds.append(region.bounds)

    def line(self, start, end, color=BLUE, opacity=0.35, width=2):
        x1, y1 = start
        x2, y2 = end
        self.elements.append(
            f'<path d="M {x1:.3f},{y1:.3f} L {x2:.3f},{y2:.3f}" '
            f'fill="none" stroke="{color}" stroke-opacity="{opacity}" '
            f'stroke-width="{width}" vector-effect="non-scaling-stroke"/>'
        )
        self.bounds.append((min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)))

    def arrow(self, start, end, color=BLUE, opacity=0.35):
        dx, dy = end[0] - start[0], end[1] - start[1]
        length = math.hypot(dx, dy)
        if not length:
            return
        angle = math.atan2(dy, dx)
        head = min(length * 0.25, 12)
        half = math.radians(20)
        stem = (
            end[0] - head * math.cos(half) * dx / length,
            end[1] - head * math.cos(half) * dy / length,
        )
        self.line(start, stem, color, opacity)
        self.region(
            Polygon(
                [
                    end,
                    *[
                        (
                            end[0] - head * math.cos(angle + sign * half),
                            end[1] - head * math.sin(angle + sign * half),
                        )
                        for sign in (-1, 1)
                    ],
                ]
            ),
            color,
            opacity,
        )


def path_svg(original: str, drawing: Drawing, crop=None) -> str:
    """Standalone path-only intermediate, in the source drawing's coordinates."""
    frame = crop
    if not frame:
        viewbox = get_root_viewbox(original)
        if viewbox is None:
            raise ValueError("Source drawing requires a viewBox when no crop is given")
        frame = tuple(map(float, viewbox.split()))
    x, y, w, h = frame
    bounds = [(x, y, x + w, y + h), *drawing.bounds]
    x0, y0 = min(b[0] for b in bounds), min(b[1] for b in bounds)
    x1, y1 = max(b[2] for b in bounds), max(b[3] for b in bounds)
    frame = f"{x0:.4f} {y0:.4f} {x1 - x0:.4f} {y1 - y0:.4f}"
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{frame}">\n'
        + "\n".join(drawing.elements)
        + "\n</svg>\n"
    )


def compose(original: str, paths: str) -> str:
    """Combine generated annotation paths with the procedure's line art."""
    ET.fromstring(original)
    ET.fromstring(paths)
    viewbox = get_root_viewbox(paths)
    if viewbox is None:
        raise ValueError("Annotation paths require a viewBox")
    original = set_root_viewbox(original, viewbox)
    index = original.rfind("</svg>")
    if index < 0:
        raise ValueError("Source drawing has no closing SVG element")
    elements = paths[paths.index(">") + 1 : paths.rfind("</svg>")]
    layer = '\n<g id="procedure-annotation">\n' + elements + "\n</g>\n"
    return original[:index] + layer + original[index:]
