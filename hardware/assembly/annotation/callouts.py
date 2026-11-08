"""Procedure-specific arrows, wiring symbols and instructional callouts.

Recipes combine CAD anchors with editorial layout offsets. They add primitives
to a Drawing using the supplied camera, without building models or writing SVG
files. Part dimensions stay imported beside the recipe that uses them.
"""

from __future__ import annotations

import math
from itertools import pairwise

from build123d import Location, Vector
from shapely import union_all
from shapely.geometry import LineString, Point, Polygon

from .geometry import select_parts, silhouette, world_shape
from .specs import BLUE, RED


def _nut_insertion(root, projection, drawing):
    nuts = [
        (nut, projection.point(world_shape(nut).bounding_box().center())[1])
        for nut in select_parts(root, "_layer_solid/TNut*")
    ]
    for extrusion in select_parts(root, "_layer_solid/Extrusion*")[:3]:
        _, y0, _, y1 = silhouette(extrusion, projection).bounds
        row = [
            silhouette(nut, projection)
            for nut, center_y in nuts
            if y0 <= center_y <= y1
        ]
        x1 = max(region.bounds[2] for region in row)
        mid = union_all(row).centroid.y
        drawing.arrow((x1 + 28, mid), (x1 + 6, mid))


def _clamp_threading(root, projection, drawing):
    from hardware.parts.custom import belt_clamp as clamp

    # Illustration offsets keep both lanes inside the slot, clear of the front lip.
    z = math.copysign(clamp.thickness / 2, projection.origin.Z)
    # Keep blue on the left when viewing either face.
    colors = (RED, BLUE) if z < 0 else (BLUE, RED)
    entry_y, return_y = 0.05, -0.85
    turn_x, turn_y = 8.8, 3.9
    knots = [
        (-9.2, entry_y),
        (entry_y, entry_y),
        (turn_y, turn_y),
        (turn_x, turn_y),
        (turn_x, return_y),
        (-8.3, return_y),
    ]
    # Quadratic bends keep the route continuous around the post. Sampling in
    # part coordinates lets both camera views share the same smooth path.
    route = [knots[0]]
    for before, corner, after in zip(knots, knots[1:], knots[2:]):
        incoming = math.dist(before, corner)
        outgoing = math.dist(corner, after)
        trim = min(0.6, incoming / 2, outgoing / 2)
        start = tuple(c + (b - c) * trim / incoming for b, c in zip(before, corner))
        end = tuple(c + (a - c) * trim / outgoing for a, c in zip(after, corner))
        for step in range(9):
            t = step / 8
            route.append(
                tuple(
                    (1 - t) ** 2 * s + 2 * (1 - t) * t * c + t**2 * e
                    for s, c, e in zip(start, corner, end)
                )
            )
    # End the stroke inside the final arrowhead so its tip stays sharp.
    route.append((knots[-1][0] + 0.5, return_y))

    for mirror, color in enumerate(colors):

        def project(point):
            x, y = point
            return projection.point((clamp.length - x if mirror else x, y, z))

        points = [project(point) for point in route]
        marks = [LineString(points).buffer(0.12), Point(points[0]).buffer(0.24)]
        # Only three direction cues: feed in, turn around the post, fold back.
        for tip, direction in (
            ((-3.8, entry_y), (1, 0)),
            ((turn_x, 1.0), (0, -1)),
            (knots[-1], (-1, 0)),
        ):
            tip_xy = project(tip)
            back_xy = project((tip[0] - direction[0], tip[1] - direction[1]))
            length = math.dist(back_xy, tip_xy)
            dx = (tip_xy[0] - back_xy[0]) / length
            dy = (tip_xy[1] - back_xy[1]) / length
            base_x, base_y = tip_xy[0] - 0.95 * dx, tip_xy[1] - 0.95 * dy
            marks.append(
                Polygon(
                    [
                        tip_xy,
                        (base_x + 0.32 * dy, base_y - 0.32 * dx),
                        (base_x - 0.32 * dy, base_y + 0.32 * dx),
                    ]
                )
            )
        ink = union_all(marks)
        # A white halo separates the route from crossing CAD edges.
        drawing.region(ink.buffer(0.10), "#ffffff", 1)
        drawing.region(ink, color, 1)


def _solenoid_wiring(root, projection, drawing):
    from hardware.parts.standard import solenoid as coil

    solenoid = select_parts(root, "Solenoid")[0]
    for region, color in zip(_lead_regions(solenoid, projection), (BLUE, RED)):
        drawing.region(region, color, 0.65)
    # Chassis-ground leader and the standard three diminishing bars.
    anchor = projection.point(
        (-coil.outer_w / 2, -coil.depth * 0.4, coil.mount_hole_z_spacing * 0.18)
    )
    end = (anchor[0] - 10, anchor[1])
    drawing.line(anchor, end, opacity=0.65)
    drawing.line(end, (end[0], end[1] + 2), opacity=0.65)
    for offset, width in ((2.3, 4.4), (3.3, 2.8), (4.3, 1.2)):
        drawing.line(
            (end[0] - width / 2, end[1] + offset),
            (end[0] + width / 2, end[1] + offset),
            opacity=0.65,
        )


def _belt_tension(root, projection, drawing):
    from hardware.assembly.belt_routing import motor_path
    from hardware.parts.standard import belt

    # The inner vertical runs are the two spans whose tension is adjusted.
    route = belt._expand_wraps(motor_path("A"))
    start, end = next(
        (a, b)
        for a, b in pairwise(route)
        if abs(a[2] - b[2]) > 100 and 0.1 < abs(a[0] - b[0]) < 2
    )
    axis = (Vector(end) - Vector(start)).normalized()
    start = Vector(start) + axis * belt.idler_pitch_r
    end = Vector(end) - axis * belt.idler_pitch_r
    for side in (-1, 1):
        drawing.region(
            Polygon(
                [
                    projection.point((side * p.X + dx, p.Y, p.Z))
                    for p, dx in (
                        (start, -belt.belt_thickness / 2),
                        (end, -belt.belt_thickness / 2),
                        (end, belt.belt_thickness / 2),
                        (start, belt.belt_thickness / 2),
                    )
                ]
            ),
            opacity=0.65,
        )
    for step, bracket in (
        ("motor_11_frame", "motor_10_bracket"),
        ("motor_21_frame", "motor_20_bracket"),
    ):
        for screw in select_parts(root, f"**/{step}/{bracket}/Screw_BHCS_M5*"):
            center = projection.point(world_shape(screw).bounding_box().center())
            drawing.region(Point(center).buffer(4.8))
    bbox = root.bounding_box()
    corners = [
        projection.point((x, y, z))
        for x in (bbox.min.X, bbox.max.X)
        for y in (bbox.min.Y, bbox.max.Y)
        for z in (bbox.min.Z, bbox.max.Z)
    ]
    width = max(x for x, _ in corners) - min(x for x, _ in corners)
    height = max(y for _, y in corners) - min(y for _, y in corners)
    drawing.arrow((0, -height * 0.10), (0, -height * 0.20))
    for sign in (-1, 1):
        drawing.arrow(
            (sign * width * 0.18, height * 0.265),
            (sign * width * 0.33, height * 0.195),
        )


def _mount_motion(root, projection, drawing):
    from hardware.parts.custom import solenoid_mount as mount_spec

    mount = select_parts(root, "SolenoidMount")[0]
    location = mount.global_location
    x0, y0, x1, y1 = silhouette(mount, projection).bounds
    join_y = mount_spec.width / 2 - mount_spec.wall_thickness
    center = projection.point(
        (
            location
            * Location(
                (
                    0,
                    join_y - mount_spec.keyboard_slot_h / 2,
                    -mount_spec.thickness / 2,
                )
            )
        ).position
    )[0]
    drawing.arrow((center + 0.2, y0 + 0.15), (center + 0.2, y0 - 10.1))
    drawing.arrow((x1 + 11.8, y1 - 6.1), (x1 + 0.4, y1 - 4.05))
    # The wiring notes refer to these two openings: the keyboard-plate slot
    # and the visible lower corner slot in the hanging screen wall.
    keyboard = [
        (-mount_spec.keyboard_slot_w / 2, join_y - mount_spec.keyboard_slot_h),
        (mount_spec.keyboard_slot_w / 2, join_y - mount_spec.keyboard_slot_h),
        (mount_spec.keyboard_slot_w / 2, join_y),
        (-mount_spec.keyboard_slot_w / 2, join_y),
    ]
    drawing.region(
        Polygon(
            [
                projection.point(
                    (location * Location((x, y, -mount_spec.thickness / 2))).position
                )
                for x, y in keyboard
            ]
        ),
        opacity=0.65,
    )
    slots = []
    for side in (-1, 1):
        x = side * (mount_spec.length / 2 - mount_spec.screen_top_slot_from_side)
        z = (
            mount_spec.thickness / 2
            + mount_spec.wall_height
            - mount_spec.screen_top_slot_from_top
        )
        points = [
            (x + dx, join_y, z + dz)
            for dx, dz in (
                (-mount_spec.screen_top_slot_w / 2, -mount_spec.screen_top_slot_h / 2),
                (mount_spec.screen_top_slot_w / 2, -mount_spec.screen_top_slot_h / 2),
                (mount_spec.screen_top_slot_w / 2, mount_spec.screen_top_slot_h / 2),
                (-mount_spec.screen_top_slot_w / 2, mount_spec.screen_top_slot_h / 2),
            )
        ]
        slots.append(
            Polygon(
                [projection.point((location * Location(p)).position) for p in points]
            )
        )
    drawing.region(max(slots, key=lambda p: p.centroid.x), opacity=0.65)
    solenoid = select_parts(root, "tapz_10_solenoid_tip/Solenoid")[0]
    for region in _lead_regions(solenoid, projection):
        drawing.region(region)


def _lead_regions(solenoid, projection):
    from hardware.parts.standard import solenoid as coil

    location = solenoid.global_location
    direction = projection.direction_in(location)
    for x in (-coil.wire_x_offset, coil.wire_x_offset):
        start_y = -coil.depth / 2
        if abs(direction.X) > 1e-6 and direction.Y < 0:
            # The near lead becomes visible at the inner shell's rear edge;
            # the far lead can be seen farther into the open coil cavity.
            edge_x = math.copysign(coil.inner_w / 2, direction.X)
            distance = (edge_x - x) / direction.X
            start_y = min(start_y, -coil.depth - distance * direction.Y)
        points = [
            projection.point((location * Location(v)).position)
            for v in (
                (x, start_y, coil.wire_z),
                (x, -coil.depth - coil.wire_length, coil.wire_z),
                (x, -coil.depth - coil.wire_length, coil.wire_z - coil.wire_drop),
            )
        ]
        yield LineString(points).buffer(
            coil.wire_radius, cap_style="flat", join_style="mitre"
        )


def _clamp_pockets(root, projection, drawing):
    from hardware.parts.custom import belt_clamp as clamp

    part = select_parts(root, "**/BeltClamp")[0]
    location = part.global_location
    # The left face exposes two horizontal nut pockets and one vertical
    # pocket in this camera. Their dimensions are the actual mating slots.
    horizontal_y = (
        clamp.thickness - clamp.left_rect1_top_offset - clamp.left_rect1_h / 2
    )
    rectangles = [
        (x, horizontal_y, clamp.left_rect1_w, clamp.left_rect1_h)
        for x in (clamp.corner_hole_offset, clamp.width - clamp.corner_hole_offset)
    ]
    rectangles.append(
        (
            clamp.width - clamp.left_rect2_right_from_front - clamp.left_rect2_w / 2,
            clamp.front_hole_offset,
            clamp.left_rect2_w,
            clamp.left_rect2_h,
        )
    )
    for x, y, width, height in rectangles:
        drawing.region(
            Polygon(
                [
                    projection.point(
                        (
                            location
                            * Location(
                                (
                                    -clamp.length / 2,
                                    clamp.width / 2 - x - dx,
                                    -clamp.thickness / 2 + y + dy,
                                )
                            )
                        ).position
                    )
                    for dx, dy in (
                        (-width / 2, -height / 2),
                        (width / 2, -height / 2),
                        (width / 2, height / 2),
                        (-width / 2, height / 2),
                    )
                ]
            ),
            opacity=0.65,
        )


def _frame_bores(root, projection, drawing):
    from hardware.assembly.procedures.frame_10_extrusion_tnut import (
        EXT_THICKNESS,
        LONG_LENGTH,
        SHORT_LENGTH,
    )
    from hardware.parts.standard.extrusion_spec import (
        bore_diameter,
        cb_end_offset,
        end_tap,
        end_tap_depth,
    )
    from hardware.parts.standard.screw import COMMON

    def circle(x, y, z, radius):
        return Polygon(
            [
                projection.point(
                    (x, y + radius * math.cos(a), z + radius * math.sin(a))
                )
                for a in (i * math.tau / 80 for i in range(80))
            ]
        )

    # Visible tap openings on the short members.
    openings = []
    for y in (-EXT_THICKNESS / 2, EXT_THICKNESS / 2):
        for z in (cb_end_offset, LONG_LENGTH - cb_end_offset):
            opening = circle(-SHORT_LENGTH / 2, y, z, bore_diameter / 2)
            openings.append(opening)
            drawing.region(opening, opacity=0.65)
    # Project the specified tapped cylinders along the actual bore axes.
    # One union keeps intersecting pale indicators at the same opacity.
    threads = []
    for side in (-1, 1):
        for y in (-EXT_THICKNESS / 2, EXT_THICKNESS / 2):
            for z in (cb_end_offset, LONG_LENGTH - cb_end_offset):
                threads.append(
                    union_all(
                        [
                            circle(
                                side * (SHORT_LENGTH / 2 - depth),
                                y,
                                z,
                                COMMON[end_tap]["d"] / 2,
                            )
                            for depth in (0, end_tap_depth)
                        ]
                    ).convex_hull
                )
    drawing.region(
        union_all(threads).difference(union_all(openings)),
        opacity=0.15,
    )


CALLOUTS = {
    "nut-insertion": _nut_insertion,
    "clamp-threading": _clamp_threading,
    "solenoid-wiring": _solenoid_wiring,
    "belt-tension": _belt_tension,
    "mount-motion": _mount_motion,
    "frame-bores": _frame_bores,
    "clamp-pockets": _clamp_pockets,
}


def draw_callouts(name: str, *, root, projection, drawing) -> None:
    """Add a named recipe to Drawing, using an already-built visible model.

    The supplied projection maps CAD anchors to 2D millimetres. This function
    mutates only the drawing; SVG serialization and file I/O belong to callers.
    """
    try:
        recipe = CALLOUTS[name]
    except KeyError:
        raise ValueError(f"Unknown illustration callout {name!r}") from None
    recipe(root, projection, drawing)
