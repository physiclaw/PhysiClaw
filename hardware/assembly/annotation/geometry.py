"""Reusable CAD-to-2D mechanics, independent of figure rules and file I/O.

Projection.point accepts world coordinates and returns SVG coordinates in
projected millimetres (Y down). Nested parts must use global_location, which
includes every parent placement. No procedure or part family belongs here.
"""

from __future__ import annotations

import math
import re
from fnmatch import fnmatchcase

import numpy as np
from build123d import Compound, Keep, Location, Plane, Vector
from OCP.BRepLib import BRepLib
from OCP.gp import gp_Ax2, gp_Vec
from OCP.HLRAlgo import HLRAlgo_Projector
from OCP.HLRBRep import HLRBRep_Algo, HLRBRep_HLRToShape
from shapely import area, polygons, union_all
from shapely.geometry import LineString, Polygon


class Projection:
    """The same camera basis OCCT uses, with ExportSVG's downward Y axis."""

    def __init__(self, camera):
        self.origin, up, target = map(Vector, camera)
        z = (self.origin - target).normalized()
        self.direction = z
        self.x = up.cross(z).normalized()
        self.y = z.cross(self.x).normalized()

    def point(self, point):
        # Project mesh vertices without allocating build123d Vector wrappers.
        relative = gp_Vec(*point)
        relative.Subtract(self.origin.wrapped)
        return relative.Dot(self.x.wrapped), -relative.Dot(self.y.wrapped)

    def direction_in(self, location):
        """Camera direction in a part's native coordinate frame."""
        return (
            location.inverse() * Location(location.position + self.direction)
        ).position


def visible_routes(root, projection, routes):
    """Project named curves with the same CAD hidden-line removal as the art.

    Query each route separately after hiding the complete scene, so foreground
    faces mask curves at their actual depth and holes remain genuine openings.
    """
    algorithm = HLRBRep_Algo()
    algorithm.Add(world_shape(root).wrapped)
    for route in routes.values():
        algorithm.Add(route.wrapped)
    axes = gp_Ax2(
        projection.origin.to_pnt(),
        projection.direction.to_dir(),
        projection.x.to_dir(),
    )
    algorithm.Projector(HLRAlgo_Projector(axes))
    algorithm.Update()
    algorithm.Hide()
    result = HLRBRep_HLRToShape(algorithm)
    visible = {}
    for name, route in routes.items():
        lines = []
        projected = result.VCompound(route.wrapped)
        if not projected.IsNull():
            # HLR edges need their 3D curves rebuilt before build123d samples them.
            BRepLib.BuildCurves3d_s(projected, 1e-6)
            for edge in Compound.cast(projected).edges():
                steps = max(2, math.ceil(edge.length / 0.15))
                points = [edge.position_at(i / steps) for i in range(steps + 1)]
                lines.append(LineString([(p.X, -p.Y) for p in points]))
        visible[name] = lines
    return visible


def select_parts(root, selector: str):
    """Resolve a CAD tree selector, refusing silently missing highlights."""
    if "|" in selector:
        return [
            part
            for option in selector.split("|")
            for part in select_parts(root, option)
        ]
    current = [root]
    for component in selector.split("/"):
        if component == "**":
            current = [part for parent in current for part in descendants(parent)]
            continue
        match = re.fullmatch(r"(.+)\[(\d+)\]", component)
        pattern = match[1] if match else component
        selected = []
        for parent in current:
            children = [c for c in parent.children if fnmatchcase(c.label, pattern)]
            if match:
                index = int(match[2])
                children = children[index : index + 1]
            selected.extend(children)
        current = selected
    if not current:
        raise ValueError(f"{root.label}: no part matches {selector!r}")
    return current


def descendants(root):
    yield root
    for child in root.children:
        yield from descendants(child)


def silhouette(shape, projection: Projection, *, fill_holes=True, tolerance=0.15):
    """Union projected triangles, optionally filling interior openings."""
    vertices, triangles = world_shape(shape).tessellate(tolerance, 0.25)
    points = np.asarray([projection.point(v) for v in vertices]).reshape(-1, 2)
    indices = np.asarray(triangles, dtype=np.intp).reshape(-1, 3)
    projected = polygons(points[indices])
    region = union_all(projected[area(projected) > 1e-7], grid_size=0.002)
    if not fill_holes:
        return region.simplify(tolerance / 3, preserve_topology=True)
    regions = list(region.geoms) if hasattr(region, "geoms") else [region]
    return union_all(
        [Polygon(p.exterior) for p in regions if isinstance(p, Polygon)]
    ).simplify(tolerance / 3, preserve_topology=True)


def world_shape(shape):
    # Rewrap the same BREP with its complete assembly placement. This avoids
    # deep-copying every face just to project a nested component in world space.
    return Compound.cast(shape.wrapped.Located(shape.global_location.wrapped))


def native_shape(shape):
    """Rewrap a part's BREP without its assembly placement."""
    return Compound.cast(shape.wrapped.Located(Location().wrapped))


def visible_face_region(face, occluder, location, projection):
    """Project a planar face, masking only material in front of its plane.

    The face and occluder share a native frame; location places both in world
    coordinates. Material behind the face must not erase its highlight.
    """
    region = silhouette(face.moved(location), projection)
    front = occluder.split(
        Plane(origin=face.center(), z_dir=face.normal_at()), Keep.TOP
    )
    if front is None:
        return region
    mask = Compound(children=front if isinstance(front, list) else [front])
    return region.difference(silhouette(mask.moved(location), projection))


def disc(part, projection, radius, z=0, *, segments=80):
    """Project a circular face in the part's native XY plane."""
    location = part.global_location
    return Polygon(
        [
            projection.point(
                (
                    location * Location((radius * math.cos(a), radius * math.sin(a), z))
                ).position
            )
            for a in (i * math.tau / segments for i in range(segments))
        ]
    )
