"""Procedure-specific arrows, wiring symbols and instructional callouts.

Recipes combine CAD anchors with editorial layout offsets. They add primitives
to a Drawing using the supplied camera, without building models or writing SVG
files. Part dimensions stay imported beside the recipe that uses them.
"""

from __future__ import annotations

import math

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

    # Entry/exit clearances differ above and below the clamp's center post.
    # Each recipe gives the side, color and route in the clamp's native XY plane.
    z = math.copysign(clamp.thickness / 2, projection.origin.Z)
    if z < 0:
        recipes = (
            (
                False,
                RED,
                [
                    ((-9, -0.2), (-5.3, -0.2)),
                    ((-4.5, -0.1), (-0.8, -0.1)),
                    ((0.5, 0.9), (3, 3.2)),
                    ((4.2, 3.9), (7.9, 3.9)),
                    ((9.5, 2.7), (9.4, -0.2)),
                    ((7.2, -1.2), (3.5, -1.2)),
                    ((1.7, -1.3), (-2, -1.3)),
                    ((-3.4, -1.3), (-7, -1.3)),
                ],
            ),
            (
                True,
                BLUE,
                [
                    ((-9.5, 0.2), (-5.9, 0.2)),
                    ((-4.9, 0.1), (-1.2, 0.1)),
                    ((0.6, 0.3), (2.7, 2.6)),
                    ((4.2, 3.8), (7.9, 3.9)),
                    ((8.6, 2.7), (8.7, -0.3)),
                    ((7.1, -1.1), (3.4, -1.1)),
                    ((2.3, -1.2), (-1.3, -1.2)),
                    ((-3.4, -1.2), (-7.1, -1.3)),
                ],
            ),
        )
    else:
        recipes = (
            (
                False,
                BLUE,
                [
                    ((-9, -0.5), (-6, -0.5)),
                    ((-5, -0.5), (-2, -0.5)),
                    ((0.8, 0.2), (2.8, 2.4)),
                    ((4.5, 3.8), (7.7, 3.8)),
                    ((8.8, 2.4), (8.8, -0.7)),
                    ((6.6, -1.5), (3, -1.5)),
                    ((0.9, -1.5), (-2.8, -1.5)),
                    ((-4.2, -1.5), (-7.8, -1.5)),
                ],
            ),
            (
                True,
                RED,
                [
                    ((-9.4, -0.3), (-5.7, -0.3)),
                    ((-5.3, -0.2), (-1.6, -0.3)),
                    ((0.7, 0.7), (3.4, 3.3)),
                    ((4.5, 4), (8.2, 3.9)),
                    ((8.9, 2.2), (9, -0.9)),
                    ((7.3, -1), (3.2, -1.1)),
                    ((0.9, -1.3), (-3.4, -1.4)),
                    ((-5.4, -1.5), (-9.7, -1.6)),
                ],
            ),
        )
    for mirror, color, path in recipes:
        for start, end in path:
            points = [
                projection.point((clamp.length - x if mirror else x, y, z))
                for x, y in (start, end)
            ]
            drawing.arrow(*points, color=color, opacity=0.65)


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
    from hardware.parts.standard import belt

    # The inner vertical runs are the two spans whose tension is adjusted.
    route = belt._expand_wraps(belt.motor_a_path)
    start, end = next(
        (a, b)
        for a, b in zip(route, route[1:])
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
