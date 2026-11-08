"""CoreXY belt routes derived from frame, pulley and carriage dimensions.

The public motor_path function returns positioned waypoints for the generic
Belt part. Motor, carriage and route calculations keep their intermediate
values local and pass immutable layouts between them.

The existing motor-B mirror remains an illustration: some mirrored waypoints
do not have physical upper-plane idlers.
"""

import math
from dataclasses import dataclass

from build123d import MM

from hardware.assembly.procedures import frame_10_extrusion_tnut as frame
from hardware.assembly.procedures.motor_10_bracket import MO10Bracket
from hardware.assembly.procedures.motor_30_pulley import LEFT_PULLEY_GAP
from hardware.assembly.travel_ranges import Y_RAIL_LENGTH
from hardware.parts.custom import belt_clamp as clamp
from hardware.parts.custom import idler_mount_front as front_idler
from hardware.parts.custom import idler_mount_motor as motor_idler
from hardware.parts.custom import xy_joint_left as joint
from hardware.parts.standard import bracket, mgn9h, motor, pulley, ring
from hardware.parts.standard.belt import _expand_wraps, belt_thickness, wrap_r
from hardware.parts.standard.extrusion import cb_end_offset

_Point = tuple[float, float, float]
_Waypoint = tuple[_Point, float | None]

# Route design choices; all machine placements below derive from part dimensions.
_RETURN_TAIL_LENGTH = 15.8 * MM
_ILLUSTRATION_TAIL_GAP = 0.2 * MM
_CENTER_CLEARANCE = 0.1 * MM
_LEAD_RADIUS = 0.8 * MM


@dataclass(frozen=True)
class _CarriageLayout:
    left_idler: _Point
    right_idler: _Point
    clamp_origin_z: float
    lower_belt_y: float
    upper_belt_y: float


@dataclass(frozen=True)
class _RoutingLayout:
    motor_a_idlers: tuple[_Waypoint, ...]
    idler_plane_shift: float
    carriage: _CarriageLayout


def _motor_a_pulley() -> _Point:
    """Belt-band center through motor_10_bracket → motor_11_frame → motor_30_pulley."""
    first_tnut_x = -frame.SHORT_LENGTH / 2 + frame.SHORT_TOP_END_GAP
    second_tnut_x = first_tnut_x + frame.SHORT_TOP_INNER_GAP
    pulley_x = (first_tnut_x + second_tnut_x) / 2

    motor_top = motor.default_height / 2
    bracket_top = motor_top + bracket.motor_plate_thick
    bracket_dx = bracket.motor_plate_length / 2 - bracket.motor_shaft_x_offset
    mounting_hole_x = (
        bracket_dx + bracket.motor_plate_length / 2 - bracket.motor_m5_x_inset
    )
    pulley_z = (frame.LONG_LENGTH - cb_end_offset) - mounting_hole_x

    spacer_height = ring.SPECS[MO10Bracket.RING_SPEC]["height"]
    motor_origin_y = -frame.EXT_THICKNESS - spacer_height + motor_top
    pulley_plane_y = motor_origin_y - bracket_top
    flange_height = (pulley.flange_belt_h - pulley.belt_width) / 2
    band_center = pulley.hub_height + flange_height + pulley.belt_width / 2
    pulley_y = pulley_plane_y - LEFT_PULLEY_GAP - band_center
    return pulley_x, pulley_y, pulley_z


def _carriage_layout(
    half_width: float, idler_band_offset: float, front_block_z: float
) -> _CarriageLayout:
    """Joint idlers and clamp planes through linear_11_y → linear_20_joint."""
    csk_left_x = -joint.length / 2 + joint.csk_hole_from_left
    csk_right_x = csk_left_x + joint.csk_x_spacing
    csk_bottom_y = -joint.width / 2 + joint.csk_hole_from_bottom
    csk_top_y = csk_bottom_y + joint.csk_y_spacing
    grid_y = (csk_bottom_y + csk_top_y) / 2
    grid_x_offset = abs((csk_left_x + csk_right_x) / 2)

    # Y rail lies between the motor-side block's bottom and front block's top.
    rail_center_z = (
        (frame.LONG_LENGTH - frame.LONG_TOP_GAP)
        - motor_idler.length / 2
        + front_block_z
        + front_idler.width / 2
    ) / 2
    slider_offset = -Y_RAIL_LENGTH / 2 + mgn9h.slider_position * Y_RAIL_LENGTH
    slider_z = rail_center_z + slider_offset
    slider_y = -frame.EXT_THICKNESS - mgn9h.block_top_z
    joint_origin_y = slider_y - joint.thickness / 2
    joint_origin_z = slider_z + grid_y
    joint_top_y = joint_origin_y - joint.thickness / 2

    # Mirrored left joint: world X = origin X + native X.
    # Right joint: world X = origin X - native X. Both map native +Y to -Z.
    left_origin_x = -half_width + grid_x_offset
    right_origin_x = half_width - grid_x_offset
    left_idler = (
        left_origin_x + joint.extra_hole2_x,
        joint_top_y - idler_band_offset,
        joint_origin_z - joint.extra_hole2_y,
    )
    right_idler = (
        right_origin_x - joint.extra_hole_x,
        joint_top_y - idler_band_offset,
        joint_origin_z - joint.extra_hole_y,
    )

    # Center the 6 mm belt in each pocket. Using an idler's Y at the lower
    # 6.2 mm pocket would put 0.25 mm of belt inside the carriage.
    slider_mount_y = -frame.EXT_THICKNESS - 2 * mgn9h.block_top_z
    return _CarriageLayout(
        left_idler=left_idler,
        right_idler=right_idler,
        clamp_origin_z=(
            joint_origin_z
            - joint.big_csk_y
            - (clamp.slot1_center_y + clamp.slot2_center_y) / 2
        ),
        lower_belt_y=slider_mount_y - clamp.belt_pocket_bottom_depth / 2,
        upper_belt_y=(
            slider_mount_y - clamp.thickness + clamp.belt_pocket_top_depth / 2
        ),
    )


def _routing_layout() -> _RoutingLayout:
    """Derive the open motor-A route and the separate motor-B idler-plane shift."""
    half_width = frame.SHORT_LENGTH / 2 + frame.EXT_THICKNESS / 2
    flange_height = (pulley.flange_belt_h - pulley.belt_width) / 2
    spacer_height = ring.SPECS["M5x8x0.5"]["height"]
    band_offset = spacer_height + flange_height + pulley.belt_width / 2
    lower_y = -frame.EXT_THICKNESS - motor_idler.thickness - band_offset
    upper_y = lower_y - pulley.flange_belt_h - spacer_height

    # LU native +X points down world Z; RU reverses it. Only the columns
    # traversed by motor A are needed; motor B mirrors this ordered route.
    motor_block_z = frame.LONG_LENGTH - frame.LONG_TOP_GAP
    lu_down1 = (-half_width, lower_y, motor_block_z + motor_idler.outer_hole_offset)
    lu_down2 = (-half_width, lower_y, motor_block_z - motor_idler.outer_hole_offset)
    ru_down2 = (half_width, lower_y, motor_block_z + motor_idler.outer_hole_offset)
    front_block_z = frame.LONG_BOT_GAP - front_idler.slot_center_y
    ld = (-half_width, lower_y, front_block_z + front_idler.top_hole_y)
    carriage = _carriage_layout(half_width, band_offset, front_block_z)

    centers = (
        carriage.left_idler,
        ld,
        lu_down2,
        _motor_a_pulley(),
        lu_down1,
        ru_down2,
        carriage.right_idler,
    )
    return _RoutingLayout(
        motor_a_idlers=tuple((point, wrap_r) for point in centers),
        idler_plane_shift=upper_y - lower_y,
        carriage=carriage,
    )


def _clamp_end(
    side: int, belt_y: float, origin_z: float, *, tail_gap: float | None = None
) -> list[_Waypoint]:
    """Free tip to working run, following the clamp's native XY channels.

    From the working run the belt climbs the diagonal pocket, turns DOWN
    around the post, then returns through the side slot. Teeth mesh in
    that slot; the smooth belt envelopes overlap there to represent the
    interlocked teeth rather than two full-thickness, separated strips.
    """
    slot_y = clamp.slot2_center_y - clamp.side_slot_from_slot2 - clamp.side_slot_w / 2
    entry_y = slot_y + belt_thickness / 4
    tail_y = slot_y - belt_thickness / 4
    if tail_gap is not None:
        # Instructional spacing is measured between belt edges, not centers.
        tail_y = entry_y - belt_thickness - tail_gap
    pocket_y = (clamp.belt_pocket_vertices[2][1] + clamp.belt_pocket_vertices[5][1]) / 2
    radius = (pocket_y - tail_y) / 2
    center_y = (pocket_y + tail_y) / 2
    # Leave 0.1 mm to the mirror plane on each side of the central gap.
    center_x = clamp.length / 2 - radius - belt_thickness / 2 - _CENTER_CLEARANCE
    points = [(center_x - _RETURN_TAIL_LENGTH, tail_y)]
    for step in range(19):
        angle = math.radians(-90 + step * 10)
        points.append(
            (center_x + radius * math.cos(angle), center_y + radius * math.sin(angle))
        )
    # Round the two 45-degree joins into the diagonal channel (y=x).
    # Sharp butt-ended segments leave visible notches on the outside corner.
    lead_radius = _LEAD_RADIUS
    tangent = lead_radius * math.tan(math.pi / 8)
    for cx, cy, start, sweep in (
        (pocket_y + tangent, pocket_y - lead_radius, 90, 45),
        (entry_y - tangent, entry_y + lead_radius, -45, -45),
    ):
        for step in range(7):
            angle = math.radians(start + sweep * step / 6)
            points.append(
                (cx + lead_radius * math.cos(angle), cy + lead_radius * math.sin(angle))
            )
    points.append((-clamp.length / 2, entry_y))
    return [
        ((side * (clamp.length / 2 - x), belt_y, origin_z + y), None) for x, y in points
    ]


def motor_path(
    motor: str, *, include_clamp: bool = True, illustrate_clamp: bool = False
) -> list[_Waypoint]:
    """Return a fresh motor-A or motor-B route in world coordinates.

    Waypoints pair a position with its wrap radius; None means an explicit
    point. include_clamp=False keeps only the working run between entries.
    illustrate_clamp=True opens the return gaps and straightens the entries
    for the manual, rather than representing the installed tooth engagement.
    """
    if motor not in ("A", "B"):
        raise ValueError(f"Belt motor must be 'A' or 'B', got {motor!r}")
    layout = _routing_layout()
    if motor == "A":
        middle = list(layout.motor_a_idlers)
        belt_y = layout.carriage.lower_belt_y
        side = -1
    else:
        middle = [
            ((-p[0], p[1] + layout.idler_plane_shift, p[2]), radius)
            for p, radius in layout.motor_a_idlers
        ]
        belt_y = layout.carriage.upper_belt_y
        side = 1
    gap = _ILLUSTRATION_TAIL_GAP if illustrate_clamp else None
    origin_z = layout.carriage.clamp_origin_z
    # Both ends initially run from free tip to entry; reverse the finish
    # only when joining them into the continuous belt route.
    start = _clamp_end(side, belt_y, origin_z, tail_gap=gap)
    finish = _clamp_end(-side, belt_y, origin_z, tail_gap=gap)
    if illustrate_clamp:
        # The manual needs an uninterrupted incoming strip. Keep its width
        # in one plane, then align each clamp approach to the actual expanded
        # idler tangent. Otherwise a tiny depth/angle change at the carriage
        # edge draws a transverse crease that looks like a splice.
        middle = [((p[0], belt_y, p[2]), radius) for p, radius in middle]
        working = _expand_wraps(start[-1:] + middle + finish[-1:])
        start = _align_clamp_entry(start, working[1])
        finish = _align_clamp_entry(finish, working[-2])
    if not include_clamp:
        return start[-1:] + middle + finish[-1:]
    return start + middle + list(reversed(finish))


def _align_clamp_entry(end: list[_Waypoint], tangent: _Point) -> list[_Waypoint]:
    """Rotate an entire clamp end about its entry, preserving tail gap/length."""
    origin = end[-1][0]
    previous = end[-2][0]
    angle = math.atan2(tangent[2] - origin[2], tangent[0] - origin[0]) - math.atan2(
        origin[2] - previous[2], origin[0] - previous[0]
    )
    cosine, sine = math.cos(angle), math.sin(angle)
    return [
        (
            (
                origin[0] + (p[0] - origin[0]) * cosine - (p[2] - origin[2]) * sine,
                p[1],
                origin[2] + (p[0] - origin[0]) * sine + (p[2] - origin[2]) * cosine,
            ),
            radius,
        )
        for p, radius in end
    ]
