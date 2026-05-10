#!/usr/bin/env python3
"""Probe a STEP file and emit a structured geometric summary.

Designed to give a build123d-coding agent enough information to reconstruct
a simple part. Does not attempt feature recognition — it surfaces raw
measurements and groups; interpretation is the agent's job.

Usage:
    python probe_step.py path/to/part.step

Output: JSON to stdout. Pipe to a file or jq if you want.
"""

from __future__ import annotations

import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

from build123d import import_step, GeomType, Axis


def _round(x: float, digits: int = 4) -> float:
    """Round and normalize -0.0 to 0.0 for cleaner output."""
    r = round(x, digits)
    return 0.0 if r == 0.0 else r


def _vec_round(v, digits: int = 4):
    return [_round(v.X, digits), _round(v.Y, digits), _round(v.Z, digits)]


def probe(step_path: str) -> dict:
    shape = import_step(step_path)

    # If it's a Compound with multiple top-level solids, flag as assembly.
    solids = shape.solids()
    is_assembly = len(solids) > 1

    bb = shape.bounding_box()
    classified = _hole_candidates(shape, bb)

    summary = {
        "file": str(step_path),
        "is_assembly": is_assembly,
        "solid_count": len(solids),
        "volume": _round(shape.volume, 3),
        "bounding_box": {
            "min": _vec_round(bb.min),
            "max": _vec_round(bb.max),
            "size": _vec_round(bb.size),
            "center": _vec_round(bb.center()),
        },
        "face_inventory": _face_inventory(shape),
        "edge_inventory": _edge_inventory(shape),
        "distinct_radii": _distinct_radii(shape, classified),
        "classified_features": classified,
        "symmetry_hints": _symmetry_hints(shape, bb),
    }
    return summary


def _face_inventory(shape) -> dict:
    """Count faces by GeomType, with extra detail for cylinders/planes."""
    by_type = Counter()
    cylinders = []
    planes_by_normal = defaultdict(list)

    for f in shape.faces():
        gt = f.geom_type
        # geom_type may be a GeomType enum or a string depending on version;
        # normalize to a string label.
        gt_label = gt.name if hasattr(gt, "name") else str(gt)
        by_type[gt_label] += 1

        if gt_label == "CYLINDER":
            try:
                axis = f.axis_of_rotation
                cylinders.append({
                    "radius": _round(f.radius, 4),
                    "axis_origin": _vec_round(axis.position),
                    "axis_direction": _vec_round(axis.direction),
                    "area": _round(f.area, 2),
                })
            except Exception:
                # Some cylindrical faces may not expose .radius cleanly.
                cylinders.append({"area": _round(f.area, 2), "note": "radius unavailable"})
        elif gt_label == "PLANE":
            try:
                n = f.normal_at()
                # Bin by rounded normal so co-planar / parallel-normal faces group.
                key = (_round(n.X, 3), _round(n.Y, 3), _round(n.Z, 3))
                planes_by_normal[str(key)].append({
                    "center": _vec_round(f.center()),
                    "area": _round(f.area, 2),
                })
            except Exception:
                pass

    return {
        "counts_by_type": dict(by_type),
        "cylinder_faces": cylinders,
        "planes_by_normal": dict(planes_by_normal),
    }


def _edge_inventory(shape) -> dict:
    """Count edges by GeomType. Circles get radii."""
    by_type = Counter()
    circle_radii = []
    line_lengths = []

    for e in shape.edges():
        gt = e.geom_type
        gt_label = gt.name if hasattr(gt, "name") else str(gt)
        by_type[gt_label] += 1

        if gt_label == "CIRCLE":
            try:
                circle_radii.append(_round(e.radius, 4))
            except Exception:
                pass
        elif gt_label == "LINE":
            line_lengths.append(_round(e.length, 4))

    return {
        "counts_by_type": dict(by_type),
        "circle_radii_distinct_sorted": sorted(set(circle_radii)),
        "line_length_distinct_sorted": sorted(set(line_lengths)),
    }


def _distinct_radii(shape, classified=None) -> dict:
    """All distinct radii from cylinder faces and circle edges.

    When classified features are provided, separately list the corner-fillet
    radii so the agent doesn't confuse them with hole radii.
    """
    cyl_radii = set()
    for f in shape.faces():
        if _gt_label(f.geom_type) == "CYLINDER":
            try:
                cyl_radii.add(_round(f.radius, 4))
            except Exception:
                pass

    edge_radii = set()
    for e in shape.edges():
        if _gt_label(e.geom_type) == "CIRCLE":
            try:
                edge_radii.add(_round(e.radius, 4))
            except Exception:
                pass

    corner_fillet_radii = set()
    hole_or_boss_radii = set()
    if classified:
        corner_fillet_radii = {g["radius"] for g in classified.get("corner_fillets", [])}
        hole_or_boss_radii = {g["radius"] for g in classified.get("holes", [])}
        hole_or_boss_radii |= {g["radius"] for g in classified.get("bosses", [])}

    # Edge-only radii (not in cylinder faces) are likely 3D fillets between
    # non-parallel surfaces (the surface is a torus, not a cylinder).
    edge_only_radii = sorted(edge_radii - cyl_radii)

    return {
        "cylinder_face_radii": sorted(cyl_radii),
        "circle_edge_radii": sorted(edge_radii),
        "corner_fillet_radii": sorted(corner_fillet_radii),
        "hole_or_boss_radii": sorted(hole_or_boss_radii),
        # Edge-only radii are likely 3D fillets (where the surface is a torus).
        # Note: derived radii from filleting circular edges (R±r) may also appear here.
        # The smallest value is most likely the real fillet parameter.
        "likely_3d_fillet_radii": edge_only_radii,
    }


def _hole_candidates(shape, bb) -> dict:
    """Classify cylindrical features into corner fillets, holes, and bosses.

    A cylindrical face's axis position relative to the bounding box reveals
    its role:
      - Axis at a vertical edge of the bbox → corner fillet of the prism
      - Axis interior to bbox, radius < part dimension → hole or boss
      - Axis on an external face of bbox → external rounded edge

    Holes have inferred depth from the difference between the cylinder face's
    axis position and the nearest perpendicular plane along the axis.
    """
    # Collect cylinder faces with metadata
    cyl_faces = []
    for f in shape.faces():
        gt_label = _gt_label(f.geom_type)
        if gt_label != "CYLINDER":
            continue
        try:
            axis = f.axis_of_rotation
            radius = _round(f.radius, 4)
            d = _snap_to_principal((
                _round(axis.direction.X, 3),
                _round(axis.direction.Y, 3),
                _round(axis.direction.Z, 3),
            ))
            cyl_faces.append({
                "face": f,
                "radius": radius,
                "direction": d,
                "position": axis.position,
                "area": _round(f.area, 2),
            })
        except Exception:
            continue

    # Collect plane Z-positions (or principal-axis positions) for depth lookup
    planes_by_axis = defaultdict(list)  # key: principal axis tuple, value: list of (position_along_axis, plane_face)
    for f in shape.faces():
        if _gt_label(f.geom_type) != "PLANE":
            continue
        try:
            n = f.normal_at()
            n_snapped = _snap_to_principal((_round(n.X, 3), _round(n.Y, 3), _round(n.Z, 3)))
            if n_snapped not in [(1,0,0),(-1,0,0),(0,1,0),(0,-1,0),(0,0,1),(0,0,-1)]:
                continue
            # Axis-aligned plane. Position along its normal axis = dot(center, n).
            c = f.center()
            pos_along = c.X * n_snapped[0] + c.Y * n_snapped[1] + c.Z * n_snapped[2]
            # Use the absolute axis (always positive) as key for grouping ±X, ±Y, ±Z
            abs_axis = tuple(abs(x) for x in n_snapped)
            planes_by_axis[abs_axis].append(pos_along)
        except Exception:
            continue

    # Group cylinders by (radius, direction)
    groups = defaultdict(list)
    for c in cyl_faces:
        key = (c["radius"], c["direction"])
        groups[key].append(c)

    corner_fillets = []
    holes = []
    bosses = []
    other = []

    for (radius, direction), members in groups.items():
        # Classify by relationship to bounding box
        # A cylinder is a corner fillet if its axis lies on a vertical edge of the bbox
        # (XY position is on a corner of the bbox footprint, axis aligned with one principal axis).
        positions = [_vec_round(m["position"]) for m in members]
        bbox_role = _classify_axis_position(members, direction, bb, radius)

        entry = {
            "radius": radius,
            "axis_direction": list(direction),
            "count": len(members),
            "axis_origins": positions,
            "looks_like_pattern": len(members) >= 3,
        }

        if bbox_role == "corner_fillet":
            corner_fillets.append(entry)
        elif bbox_role in ("hole", "boss"):
            # Estimate depth: distance from cylinder axis position to nearest
            # perpendicular plane along the cylinder axis direction.
            entry["inferred_depth_range"] = _estimate_depth(members, direction, planes_by_axis)
            if bbox_role == "hole":
                holes.append(entry)
            else:
                bosses.append(entry)
        else:
            other.append(entry)

    return {
        "corner_fillets": sorted(corner_fillets, key=lambda g: g["radius"]),
        "holes": sorted(holes, key=lambda g: (-g["count"], g["radius"])),
        "bosses": sorted(bosses, key=lambda g: (-g["count"], g["radius"])),
        "other_cylinders": sorted(other, key=lambda g: g["radius"]),
    }


def _classify_axis_position(members, direction, bb, radius) -> str:
    """Decide whether a group of cylinder faces represents:
      - corner_fillet: axes lie on bbox vertical edges, radius small
      - hole: axes interior to bbox footprint
      - boss: axes interior, but cylinder extends beyond bbox face
      - other: anything that doesn't fit
    """
    # Reject if direction isn't a principal axis (we don't classify oblique features)
    is_principal = direction in [(1,0,0),(-1,0,0),(0,1,0),(0,-1,0),(0,0,1),(0,0,-1)]
    if not is_principal:
        return "other"

    # For each member, project axis position onto the plane perpendicular to direction.
    # If that 2D position is at a corner of the bbox footprint (within radius tol),
    # it's a corner fillet.
    abs_axis = tuple(abs(x) for x in direction)
    perp_axes = [i for i in range(3) if abs_axis[i] == 0]  # the two perpendicular axes

    bbox_min = [bb.min.X, bb.min.Y, bb.min.Z]
    bbox_max = [bb.max.X, bb.max.Y, bb.max.Z]

    corner_count = 0
    interior_count = 0
    for m in members:
        pos = [m["position"].X, m["position"].Y, m["position"].Z]
        # Distance from axis position to each of the 4 corners of the bbox footprint
        # (in the perpendicular plane).
        i, j = perp_axes
        # The 4 corners in (perp_i, perp_j):
        corners = [
            (bbox_min[i], bbox_min[j]),
            (bbox_min[i], bbox_max[j]),
            (bbox_max[i], bbox_min[j]),
            (bbox_max[i], bbox_max[j]),
        ]
        # If axis position is within tolerance of `√2 * radius` from a corner,
        # it's a corner fillet (the fillet axis sits at corner ∓ (R, R)).
        min_d = min(((pos[i] - cx)**2 + (pos[j] - cy)**2) ** 0.5 for cx, cy in corners)
        sqrt2 = 2 ** 0.5
        # Corner fillet: axis is at distance ≈ √2·radius from a corner.
        # Hole/boss: axis is well interior (> 2·radius from any corner).
        if abs(min_d - sqrt2 * radius) < radius * 0.25:
            corner_count += 1
        elif min_d > radius * 2.5:
            interior_count += 1

    if corner_count >= len(members) * 0.75 and corner_count >= 3:
        # Majority are at corners, and we have at least 3 (a prism has 4) → corner fillets
        return "corner_fillet"
    if interior_count >= len(members) * 0.75:
        # Majority interior. Distinguish hole vs boss is hard from probe alone;
        # heuristic: if cylinder's axis range stays within bbox along its direction,
        # it's likely a hole (subtractive). If it extends beyond bbox, it's a boss.
        # We approximate by checking if the axis position is at a bbox face along the direction.
        return _hole_or_boss(members, direction, bbox_min, bbox_max)
    return "other"


def _hole_or_boss(members, direction, bbox_min, bbox_max) -> str:
    """Distinguish holes from bosses by surface normal direction.

    A cylindrical face has two valid orientations:
      - Hole: surface normal points TOWARD the axis (inward) — material is outside
      - Boss: surface normal points AWAY from axis (outward) — material is inside

    We sample a point on each face and check whether the normal at that point
    points toward or away from the axis position.
    """
    boss_votes = 0
    hole_votes = 0
    for m in members:
        try:
            f = m["face"]
            # Sample point in the middle of the face's parameter space
            n = f.normal_at()  # outward-pointing surface normal at the parameter center
            center = f.center()
            axis_pos = m["position"]
            # Vector from axis to face center, projected onto plane perpendicular to axis
            # (we don't care about the axial component — only radial direction).
            radial = (center.X - axis_pos.X, center.Y - axis_pos.Y, center.Z - axis_pos.Z)
            # Subtract the axis-direction component
            ax = direction
            dot = radial[0]*ax[0] + radial[1]*ax[1] + radial[2]*ax[2]
            radial_perp = (radial[0] - dot*ax[0], radial[1] - dot*ax[1], radial[2] - dot*ax[2])
            # Dot product of normal with radial vector:
            #   positive → normal points outward (away from axis) → boss
            #   negative → normal points inward (toward axis) → hole
            n_dot_r = n.X*radial_perp[0] + n.Y*radial_perp[1] + n.Z*radial_perp[2]
            if n_dot_r > 0:
                boss_votes += 1
            elif n_dot_r < 0:
                hole_votes += 1
        except Exception:
            continue

    if boss_votes == hole_votes == 0:
        return "hole"  # default fallback
    return "boss" if boss_votes > hole_votes else "hole"


def _estimate_depth(members, direction, planes_by_axis) -> dict:
    """For a hole/boss, estimate axial extent by looking at planes along the axis.

    Returns the cylinder height inferred from face area, which for axis-aligned
    cylinders equals area / (2πr).
    """
    heights = []
    for m in members:
        if m["radius"]:
            h = m["area"] / (2 * 3.14159265 * m["radius"])
            heights.append(round(h, 3))
    return {
        "from_face_area": sorted(set(heights)),
    }


def _gt_label(gt) -> str:
    return gt.name if hasattr(gt, "name") else str(gt)


def _snap_to_principal(d: tuple, tol: float = 0.01) -> tuple:
    """If a direction is within tol of a principal axis, snap it. Otherwise leave."""
    for principal in [(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)]:
        if all(abs(d[i] - principal[i]) < tol for i in range(3)):
            return principal
    return d


def _symmetry_hints(shape, bb) -> dict:
    """Cheap heuristics: is the bounding box centered? Is there an obvious
    axis of rotational symmetry?"""
    center = bb.center()
    centered_on_origin = all(abs(c) < 1e-3 for c in [center.X, center.Y, center.Z])

    size = bb.size
    # Square bounding box footprints are a hint at 4-fold symmetry; equal X=Y=Z hints at higher.
    xy_square = abs(size.X - size.Y) < 1e-3
    xz_square = abs(size.X - size.Z) < 1e-3
    yz_square = abs(size.Y - size.Z) < 1e-3

    return {
        "centered_on_origin": centered_on_origin,
        "bbox_xy_square": xy_square,
        "bbox_xz_square": xz_square,
        "bbox_yz_square": yz_square,
    }


def main():
    if len(sys.argv) != 2:
        print("usage: python probe_step.py <path-to-step-file>", file=sys.stderr)
        sys.exit(2)

    step_path = sys.argv[1]
    if not Path(step_path).exists():
        print(f"file not found: {step_path}", file=sys.stderr)
        sys.exit(2)

    summary = probe(step_path)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
