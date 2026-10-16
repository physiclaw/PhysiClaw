"""Refit a patch's highlight polygons from the model.

A highlight traced by hand in the mark UI is geometry frozen against one
render; when the part it outlines moves or changes shape, the polygon
silently stops fitting. This tool recomputes each highlight as the exact
projected silhouette of the parts it covers, using the same camera the
render used, and replays the patch:

    uv run --group cad python -m hardware refit \\
        hardware/assembly/patch/linear_10_y_exploded_cam0.json --parts Screw TNut

``--parts`` names the top-level children of the step's compound to
outline, by label prefix (``Screw`` matches ``Screw_BHCS_M3x8``). The
selection is recorded on the op as ``parts`` so a later refit of the same
patch needs no arguments. Every op in the patch is refitted; an op with no
``parts`` and no ``--parts`` is left alone.

A screw is outlined as its head and shank, a hammer T-nut as its plate and
boss — each a convex solid whose projection is the convex hull of its
edges, so the union of the hulls is the exact silhouette without relying
on hidden-line loops closing. Any other part (a whole sub-assembly, say)
is outlined by polygonizing its visible edges.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from build123d import Align, Box, GeomType, Location, Shape
from shapely.geometry import LineString, MultiPolygon, Polygon
from shapely.ops import polygonize, unary_union

from hardware import buildlog
from hardware.assembly.dispatch import load_class
from hardware.assembly.mark.patch import load_patch, make_entry, write_patch
from hardware.assembly.mark.replay import replay_one
from hardware.assembly.mark.validate import validate_shapes
from hardware.assembly.projection import camera_view
from hardware.parts.standard.t_nut import HALF_PROFILES

STEM_RE = re.compile(
    r"^(?P<module>.+)_(?P<variant>exploded|assembled)_cam(?P<cam>\d+)$"
)
CURVE_STEP = 0.3  # mm between samples along a curved projected edge
TOLERANCE = 0.1  # mm — simplify the outline to this (invisible at print scale)
DECIMALS = 3  # stored coordinate precision, mm

# Where a part splits into convex pieces, as a height above its location
# origin: a screw at its underhead plane (local z = 0), a hammer T-nut at
# its plate top. Parts not listed are outlined whole.
SPLIT_ABOVE_ORIGIN = {
    "Screw": 0.0,
    "TNut_hammer": HALF_PROFILES["hammer"][-1][1],
}


def _edges(shape: Shape, cam_pos, up, look_at) -> list[LineString]:
    """The visible projected edges of ``shape`` as polylines: endpoints of
    straight edges, ``CURVE_STEP`` samples along curved ones."""
    visible, _ = shape.project_to_viewport(cam_pos, up, look_at=look_at)
    out = []
    for e in visible:
        n = (
            2
            if e.geom_type == GeomType.LINE
            else max(3, int(e.length / CURVE_STEP) + 1)
        )
        out.append(
            LineString(
                [(p.X, p.Y) for p in (e.position_at(i / (n - 1)) for i in range(n))]
            )
        )
    return out


def _convex_pieces(shape: Shape, z: float) -> list[Shape]:
    """The part cut by the horizontal plane at world ``z``, as two solids."""
    c = shape.bounding_box().center()
    size = shape.bounding_box().diagonal * 2
    below = Box(size, size, size, align=(Align.CENTER, Align.CENTER, Align.MAX))
    above = Box(size, size, size, align=(Align.CENTER, Align.CENTER, Align.MIN))
    at = Location((c.X, c.Y, z))
    return [shape.intersect(below.moved(at)), shape.intersect(above.moved(at))]


def silhouette(shape: Shape, cam_pos, up, look_at) -> Polygon:
    label = getattr(shape, "label", "")
    for prefix, lift in SPLIT_ABOVE_ORIGIN.items():
        if label.startswith(prefix):
            z = shape.location.position.Z + lift
            hulls = [
                unary_union(_edges(piece, cam_pos, up, look_at)).convex_hull
                for piece in _convex_pieces(shape, z)
            ]
            return Polygon(unary_union(hulls).exterior)
    # A non-convex part: the regions its visible edges enclose, merged.
    merged = unary_union(_edges(shape, cam_pos, up, look_at))
    region = unary_union([p.buffer(0.03) for p in polygonize(merged)])
    if isinstance(region, MultiPolygon):
        region = max(region.geoms, key=lambda g: g.area)
    if region.is_empty:
        raise ValueError(
            f"{label}: its visible edges enclose no region from this camera"
        )
    return Polygon(region.exterior)


def _root_points(poly: Polygon) -> list[list[float]]:
    """Projected (x, y) → SVG root coordinates: ExportSVG flips y."""
    poly = poly.simplify(TOLERANCE)
    return [
        [round(x, DECIMALS), round(-y, DECIMALS)] for x, y in poly.exterior.coords[:-1]
    ]


def refit(patch: Path, parts: list[str] | None) -> int:
    """Refit every op of ``patch`` whose parts are known. Returns the
    number of ops refitted; replays the patch when any were."""
    m = STEM_RE.match(patch.stem)
    if not m:
        raise ValueError(f"{patch.name}: not a <procedure>_<variant>_cam<i> patch")
    asm = load_class(m["module"])(exploded=m["variant"] == "exploded")
    compound = asm.build()
    cam_pos, up, look_at = camera_view(compound, asm.cameras[int(m["cam"])])
    src = asm.svg_path(index=int(m["cam"]))

    ops = load_patch(src)
    done = 0
    for op in ops:
        wanted = parts or op.get("parts")
        if not wanted:
            continue
        chosen = [
            c
            for c in compound.children
            if getattr(c, "label", "").startswith(tuple(wanted))
        ]
        if not chosen:
            raise ValueError(
                f"{patch.name}: no child labelled {wanted} in {asm.compound_label}"
            )
        style = {k: v for k, v in op["shapes"][0].items() if k not in ("type", "geom")}
        shapes = [
            {
                "type": "polygon",
                "geom": {"points": _root_points(silhouette(c, cam_pos, up, look_at))},
                **style,
            }
            for c in chosen
        ]
        entry = make_entry(
            op["id"], op["preop"], validate_shapes(shapes), op["viewBox"]
        )
        entry["parts"] = list(wanted)
        op.clear()
        op.update(entry)
        buildlog.event(
            "highlight_refitted",
            "",
            item=f"{patch.stem}/{op['id']}",
            highlights=len(shapes),
            parts=wanted,
        )
        done += 1
    if done:
        write_patch(src, ops)
        for out in replay_one(src):
            buildlog.event("artifact_written", out.name, level="debug", path=str(out))
    return done


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("patch", type=Path, help="hardware/assembly/patch/<stem>.json")
    ap.add_argument("--parts", nargs="+", help="label prefixes of the parts to outline")
    buildlog.add_arguments(ap)
    args = ap.parse_args(argv[1:])
    with buildlog.command("Hardware annotation refit", args, name="refit") as run:
        count = refit(args.patch.resolve(), args.parts)
        if not count:
            buildlog.event(
                "refit_error",
                "No op has parts to refit (pass --parts)",
                level="error",
                path=str(args.patch),
            )
            run.exit_code = 2
        run.summary = f"{count} operations"
        return run.exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
