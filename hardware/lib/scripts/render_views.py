"""Pure-Python wireframe-SVG renderer for FreeCAD Part shapes.

Headless and dependency-free — runs inside FreeCAD's embedded interpreter
without GUI or matplotlib. Each generated `.FCStd` is projected to a
handful of standard views and written under `hardware/views/`. Useful for
spotting "the script ran but the geometry is wrong" issues without
opening the FreeCAD GUI.

Convention: +Z is up, +X is right, +Y is into the screen at azim=0.

    iso    35.264° elev, 45° azim   true isometric
    top    90°    elev, 0°  azim    looking down -Z
    front  0°     elev, 0°  azim    looking along +Y (Y disappears)
    right  0°     elev, 90° azim    looking along -X (X disappears)
"""

import math
from pathlib import Path


# (elev_deg, azim_deg)
DEFAULT_VIEWS = {
    "iso":   (35.264, 45.0),
    "top":   (90.0,    0.0),
    "front": (0.0,     0.0),
    "right": (0.0,    90.0),
}


def _project(p, elev_rad, azim_rad):
    """Project a (x, y, z) point onto a 2D screen plane.

    First rotates the world about +Z by `azim`, then tilts about screen-X
    by `elev`. Returns (sx, sy).
    """
    ca, sa = math.cos(azim_rad), math.sin(azim_rad)
    ce, se = math.cos(elev_rad), math.sin(elev_rad)
    x1 = p.x * ca + p.y * sa
    y1 = -p.x * sa + p.y * ca
    z1 = p.z
    sx = x1
    sy = y1 * se + z1 * ce
    return sx, sy


def _discretize_edges(shape, curved_samples=48):
    """Sample each edge into 3D points. Straight lines need 2 points; curves
    are sampled with `curved_samples`. Returns a list of point lists.
    """
    edge_pts = []
    for edge in shape.Edges:
        # Straight edge → endpoints suffice.
        is_line = type(edge.Curve).__name__ == "Line"
        n = 2 if is_line else curved_samples
        try:
            pts = edge.discretize(Number=n)
        except Exception:
            pts = [v.Point for v in edge.Vertexes] if edge.Vertexes else []
        if len(pts) >= 2:
            edge_pts.append(pts)
    return edge_pts


def _project_polylines(edge_pts, elev_rad, azim_rad):
    return [[_project(p, elev_rad, azim_rad) for p in pts] for pts in edge_pts]


def _render_polylines(polylines, out_path, elev_deg, azim_deg,
                      width, height, margin, stroke, label):
    if not polylines:
        Path(out_path).write_text(
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
            f'height="{height}"><text x="20" y="40" font-family="monospace" '
            f'font-size="14">empty shape</text></svg>'
        )
        return

    xs = [p[0] for poly in polylines for p in poly]
    ys = [p[1] for poly in polylines for p in poly]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    span = max(maxx - minx, maxy - miny, 1e-6)
    scale = (min(width, height) - 2 * margin) / span
    cx, cy = (minx + maxx) / 2.0, (miny + maxy) / 2.0

    def to_screen(p):
        x = (p[0] - cx) * scale + width / 2.0
        y = height / 2.0 - (p[1] - cy) * scale  # SVG y goes down
        return f"{x:.2f},{y:.2f}"

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}">',
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
    ]
    for poly in polylines:
        pts = " ".join(to_screen(p) for p in poly)
        parts.append(
            f'<polyline points="{pts}" fill="none" stroke="#111" '
            f'stroke-width="{stroke}" stroke-linecap="round" '
            f'stroke-linejoin="round"/>'
        )
    caption = label or f"elev={elev_deg:g}deg azim={azim_deg:g}deg"
    bbox_w = maxx - minx
    bbox_h = maxy - miny
    parts.append(
        f'<text x="{margin}" y="{height - margin}" font-family="monospace" '
        f'font-size="11" fill="#444">{caption}</text>'
    )
    parts.append(
        f'<text x="{width - margin}" y="{height - margin}" '
        f'text-anchor="end" font-family="monospace" font-size="11" '
        f'fill="#888">{bbox_w:.1f} x {bbox_h:.1f} mm</text>'
    )
    parts.append("</svg>")

    Path(out_path).write_text("\n".join(parts), encoding="utf-8")


def render_shape(shape, out_path, elev_deg, azim_deg,
                 width=480, height=480, margin=18, stroke=0.6,
                 label=None):
    """Render a shape to a single SVG wireframe (one-shot — discretizes inline)."""
    edge_pts = _discretize_edges(shape)
    polylines = _project_polylines(
        edge_pts, math.radians(elev_deg), math.radians(azim_deg)
    )
    _render_polylines(polylines, out_path, elev_deg, azim_deg,
                      width, height, margin, stroke, label)


def render_part_views(shape, output_dir, name, views=None):
    """Write one SVG per view for `shape`. Returns the list of paths.

    Edges are discretized once and reused for every view — projection is
    cheap, discretization is not.
    """
    if views is None:
        views = DEFAULT_VIEWS
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    edge_pts = _discretize_edges(shape)
    written = []
    for view_name, (elev_deg, azim_deg) in views.items():
        out = output_dir / f"{name}_{view_name}.svg"
        polylines = _project_polylines(
            edge_pts, math.radians(elev_deg), math.radians(azim_deg)
        )
        _render_polylines(
            polylines, out, elev_deg, azim_deg, 480, 480, 18, 0.6,
            f"{name}  {view_name}  (elev={elev_deg:g}deg azim={azim_deg:g}deg)",
        )
        written.append(out)
    return written
