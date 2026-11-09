"""Part-aware CAD feature rules returning projected Shapely regions.

Each rule owns one feature family (belt runs, pockets, seats, etc.). Editorial
visibility adjustments live beside that family. All regions share the view's
projection; this module does not draw SVGs, build assemblies or write files.
"""

from __future__ import annotations

import math
import re
from functools import partial

from build123d import Compound, GeomType, Keep, Location, Plane, Vector
from shapely import union_all
from shapely.geometry import Polygon

from hardware.assembly.annotation.geometry import (
    descendants,
    disc,
    native_shape,
    select_parts,
    silhouette,
    visible_face_region,
    world_shape,
)


def _board_front(part, projection):
    """Visible board outline and components above its front edge."""
    from hardware.parts.standard.board import board_x, board_y

    location = part.global_location

    # Components behind the PCB's front edge are hidden by the board.
    # Its exposed thickness remains visible along the left outline.
    board_plane = Polygon(
        [
            projection.point((location * Location((x, y, 0))).position)
            for x, y in (
                (-board_x / 2, -board_y / 2),
                (-board_x / 2, board_y / 2),
                (board_x / 2, board_y / 2),
                (board_x / 2, -board_y / 2),
            )
        ]
    )
    return silhouette(part, projection).difference(board_plane)


def _joint_pocket(part, projection, *, feature):
    """Nut-pocket opening on a front or slanted joint face."""
    from hardware.parts.custom import xy_joint_left as joint

    location = part.global_location

    slant = feature.startswith("slant")
    if slant:
        center = joint.slant_origin + joint.slant_x_dir * joint.slant_pocket_face_x
        axis = joint.slant_x_dir
    else:
        center = Vector(joint.extra_hole_x, -joint.width / 2, 0)
        axis = Vector(1, 0, 0)
    points = []
    for u, v in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
        p = center + axis * (u * joint.pocket_w / 2)
        p.Z = joint.pocket_face_y + v * joint.pocket_h / 2
        if feature.endswith("right"):
            p.X = -p.X
        points.append(projection.point((location * Location(p)).position))
    return Polygon(points)


def _top_face(part, projection):
    """Top face of a driver slot."""
    location = part.global_location
    native = native_shape(part)
    top = native.bounding_box().max.Z
    faces = [
        face.moved(location)
        for face in native.faces()
        if abs(face.center().Z - top) < 1e-5 and face.normal_at().Z > 0.99
    ]
    return union_all([silhouette(face, projection) for face in faces])


def _hosting_extrusion(part, center):
    """Find the beam containing a seated feature's world-space center."""
    root = part
    while root.parent is not None:
        root = root.parent
    candidates = []
    for beam in descendants(root):
        if not beam.label.startswith("Extrusion"):
            continue
        point = (beam.global_location.inverse() * Location(center)).position
        bounds = native_shape(beam).bounding_box()
        if all(
            getattr(bounds.min, axis)
            <= getattr(point, axis)
            <= getattr(bounds.max, axis)
            for axis in ("X", "Y", "Z")
        ):
            candidates.append(beam)
    [beam] = candidates
    return beam


def _frame_tnut(part, projection):
    """Visible faces of a seated frame nut, including its exposed shoulders."""
    location = part.global_location
    native = native_shape(part)
    center = (location * Location(native.bounding_box().center())).position
    beam = _hosting_extrusion(part, center)
    occluder = world_shape(beam).moved(location.inverse())
    direction = projection.direction_in(location)
    return union_all(
        [
            visible_face_region(face, occluder, location, projection)
            for face in native.faces().filter_by(GeomType.PLANE)
            if face.normal_at().dot(direction) > 1e-6
        ]
    )


def _frame_head_recess(part, projection):
    """Counterbore walls exposed through the near T-slot lips.

    The recess intersects the slot, so its visible cut is not a circular disc.
    The deeper walls and seating floor are hidden by the lips in this view.
    """
    from hardware.parts.standard.extrusion_spec import cb_head_d, slot_lip_under_y

    location = part.global_location
    native = native_shape(part)
    direction = projection.direction_in(location)
    if direction.Y <= 0:
        return Polygon()
    return union_all(
        [
            silhouette(face.moved(location), projection, tolerance=0.005)
            for face in native.faces().filter_by(GeomType.CYLINDER)
            if face.radius is not None
            and abs(face.radius - cb_head_d / 2) < 1e-6
            and face.bounding_box().min.Y >= slot_lip_under_y - 1e-6
            and face.normal_at().dot(direction) > 0
        ]
    )


def _beam_tnut(part, projection):
    """Exposed crown and near end of a standard nut inside the 1020 beam."""
    [beam] = select_parts(part.parent, "Extrusion1020*")
    location = part.global_location
    native = native_shape(part)
    bounds = native.bounding_box()
    beam_native = world_shape(beam).moved(location.inverse())
    regions = []
    for axis in ("Y", "Z"):
        top = getattr(bounds.max, axis)
        faces = [
            face
            for face in native.faces()
            if abs(getattr(face.center(), axis) - top) < 1e-5
            and getattr(face.normal_at(), axis) > 0.99
        ]
        regions.append(
            union_all(
                [
                    visible_face_region(face, beam_native, location, projection)
                    for face in faces
                ]
            )
        )
    return union_all(regions)


def _stack_seat(part, projection):
    """Receiving bore under an assembled idler stack."""
    from hardware.parts._fits import M4_NORMAL

    return disc(part, projection, M4_NORMAL / 2)


def _countersink(part, projection, *, feature):
    """Visible wall of a joint countersink or an underside mounting countersink."""
    from hardware.parts.standard.screw import FHCS_DIMS, head_skirt

    location = part.global_location

    size = re.search(r"_M(\d+(?:\.\d+)?)", part.label)[1]
    head = FHCS_DIMS[f"M{size}"]
    top = head["k"] + head_skirt
    if feature == "joint-countersink":
        from hardware.parts.custom import xy_joint_left as joint

        outer = joint.big_csk_head_diameter / 2
        inner = joint.big_csk_hole_diameter / 2
        bottom = top - (outer - inner) / math.tan(math.radians(joint.csk_angle / 2))
        return disc(part, projection, outer, top).difference(
            disc(part, projection, inner, bottom)
        )
    # Only the inward-facing half of a countersink is visible from below
    # the plate. Project its actual conical wall rather than a flat disc.
    from hardware.parts._fits import M5_NORMAL

    outer, inner = head["dk"] / 2, M5_NORMAL / 2
    bottom = top - (outer - inner)
    direction = projection.direction_in(location)
    polygons = []
    for i in range(160):
        a, b = i * math.tau / 160, (i + 1) * math.tau / 160
        mid = (a + b) / 2
        if Vector(-math.cos(mid), -math.sin(mid), 1).dot(direction) <= 0:
            continue
        polygons.append(
            Polygon(
                [
                    projection.point(
                        (
                            location * Location((r * math.cos(t), r * math.sin(t), z))
                        ).position
                    )
                    for r, z, t in (
                        (outer, top, a),
                        (outer, top, b),
                        (inner, bottom, b),
                        (inner, bottom, a),
                    )
                ]
            )
        )
    return union_all(polygons)


def _mount_opening(part, projection, *, feature):
    """Visible inner wall of a SolenoidMount plate bore or countersink."""
    from hardware.assembly.procedures.tapz_20_solenoid_mount import (
        FHCS_EXPLODE,
        FHCS_HEAD_HEIGHT,
        SCREW_EXPLODE,
    )
    from hardware.parts.custom import solenoid_mount as mount

    if feature == "plate-bore":
        outer = inner = mount.keyboard_hole_diameter / 2
        top = -SCREW_EXPLODE
        bottom = top - mount.thickness
    else:
        outer = mount.screen_corner_csk_head_diameter / 2
        inner = mount.screen_corner_csk_hole_diameter / 2
        top = FHCS_HEAD_HEIGHT - FHCS_EXPLODE
        bottom = top - (outer - inner) / math.tan(
            math.radians(mount.screen_corner_csk_angle / 2)
        )
    entry = disc(part, projection, outer, top)
    exit = disc(part, projection, inner, bottom)
    location = part.global_location
    axis = (location * Location((0, 0, 1))).position - location.position
    return (
        entry.difference(exit)
        if axis.dot(projection.direction) > 0
        else exit.difference(entry)
    )


def _camera_mount(part, projection):
    """Camera assembly outline, excluding hardware buried in the frame."""
    shapes = []
    for leaf in descendants(part):
        if leaf.children or leaf.label.startswith("TNut"):
            continue
        if leaf.label.startswith("Screw_BHCS_M5"):
            # The frame-side shank is hidden; its head remains visible.
            native = native_shape(leaf)
            shapes.append(native.split(Plane.XY, Keep.TOP).moved(leaf.global_location))
        else:
            shapes.append(world_shape(leaf))
    return silhouette(Compound(children=shapes), projection)


def _receiving_seat(part, projection, *, feature):
    """Receiving marker on the frame's world -Y face or a slider mating plane."""
    location = part.global_location
    if feature == "frame-seat":
        from hardware.assembly.procedures.frame_10_extrusion_tnut import (
            EXT_THICKNESS,
        )
        from hardware.parts.standard.extrusion_spec import leg, slot_lip_under_y
        from hardware.parts.standard.t_nut import ENGAGEMENT_Y, HALF_PROFILES

        origin = location.position
        axis = (location * Location((0, 0, 1))).position - origin
        # The standard T-nut bore opens below the extrusion's outer face.
        nut_crown = HALF_PROFILES["standard"][-1][1] - ENGAGEMENT_Y["standard"]
        recess = leg - slot_lip_under_y - nut_crown
        z = (-EXT_THICKNESS + recess - origin.Y) / axis.Y
    else:
        from hardware.parts.custom.belt_clamp import slot_depth, thickness

        z = -(thickness - slot_depth)
    radius = float(re.search(r"_M(\d+(?:\.\d+)?)", part.label)[1]) / 2
    if feature == "slider-seat":
        from hardware.parts.standard.mgn9h import mount_hole_chamfer, mount_hole_dia

        radius = mount_hole_dia / 2 + mount_hole_chamfer
    region = disc(part, projection, radius, z)
    if feature == "frame-seat":
        center = (location * Location((0, 0, z))).position
        beam = _hosting_extrusion(part, center)
        # The screw's +Z points outboard. Only the slot lip ahead of the
        # recessed crown can hide its opening; cavity material behind cannot.
        front = (
            world_shape(beam)
            .moved(location.inverse())
            .split(Plane(origin=(0, 0, z)), Keep.TOP)
        )
        if front is not None:
            front = Compound(children=front if isinstance(front, list) else [front])
            mask = silhouette(front.moved(location), projection)
            if region.intersection(mask).area > 1e-7:
                region = region.difference(mask)
    return region


def _mount_seat(part, projection):
    """Solenoid-mount opening receiving an exploded screw."""
    from hardware.assembly.procedures.tapz_11_solenoid_attach import SCREW_EXPLODE
    from hardware.parts.custom.solenoid_mount import screen_pattern_hole_diameter

    return disc(
        part, projection, screen_pattern_hole_diameter / 2, -SCREW_EXPLODE, segments=40
    )


def _shaft_flat(part, projection):
    """Motor D-flat that receives the pulley grub screw."""
    from hardware.parts.standard.motor import (
        d_cut_flat_dist,
        d_cut_length,
        rod_diameter,
    )

    location = part.global_location

    # Highlight the D-flat that receives the pulley grub screw.
    native = native_shape(part)
    bounds = native.bounding_box()
    top = bounds.max.Z
    half_width = math.sqrt((rod_diameter / 2) ** 2 - d_cut_flat_dist**2)
    return Polygon(
        [
            projection.point((location * Location((x, d_cut_flat_dist, z))).position)
            for x, z in (
                (-half_width, top - d_cut_length),
                (half_width, top - d_cut_length),
                (half_width, top),
                (-half_width, top),
            )
        ]
    )


def _solenoid_mount_holes(part, projection):
    """The two mounting openings on the solenoid side wall."""
    from hardware.parts.standard import solenoid as coil

    location = part.global_location

    return union_all(
        [
            Polygon(
                [
                    projection.point(
                        (
                            location
                            * Location(
                                (
                                    -coil.outer_w / 2,
                                    -coil.depth / 2
                                    + coil.mount_hole_dia / 2 * math.cos(a),
                                    z + coil.mount_hole_dia / 2 * math.sin(a),
                                )
                            )
                        ).position
                    )
                    for a in (i * math.tau / 40 for i in range(40))
                ]
            )
            for z in (-coil.mount_hole_z_spacing / 2, coil.mount_hole_z_spacing / 2)
        ]
    )


FEATURES = {
    "silhouette": silhouette,
    "surface": partial(silhouette, fill_holes=False),
    "board-front": _board_front,
    "front-pocket": partial(_joint_pocket, feature="front-pocket"),
    "front-pocket-right": partial(_joint_pocket, feature="front-pocket-right"),
    "slant-pocket": partial(_joint_pocket, feature="slant-pocket"),
    "slant-pocket-right": partial(_joint_pocket, feature="slant-pocket-right"),
    "top-face": _top_face,
    "frame-tnut": _frame_tnut,
    "frame-head-recess": _frame_head_recess,
    "beam-tnut": _beam_tnut,
    "stack-seat": _stack_seat,
    "joint-countersink": partial(_countersink, feature="joint-countersink"),
    "countersink-wall": partial(_countersink, feature="countersink-wall"),
    "exploded-countersink": partial(_mount_opening, feature="exploded-countersink"),
    "camera-mount": _camera_mount,
    "frame-seat": partial(_receiving_seat, feature="frame-seat"),
    "slider-seat": partial(_receiving_seat, feature="slider-seat"),
    "mount-seat": _mount_seat,
    "plate-bore": partial(_mount_opening, feature="plate-bore"),
    "shaft": _shaft_flat,
    "mount-holes": _solenoid_mount_holes,
}


def feature_region(part, projection, feature):
    """Resolve a declared feature name to its projected region."""
    try:
        rule = FEATURES[feature]
    except KeyError:
        raise ValueError(f"Unknown highlight feature {feature!r}") from None
    return rule(part, projection)


def occlusion_region(root, projection, selector, *, fill_slots=False):
    """Project foreground leaves; optionally close extrusion slot-mouth gaps."""
    regions = []
    for occluder in select_parts(root, selector):
        for leaf in descendants(occluder):
            if leaf.children:
                continue
            region = silhouette(leaf, projection)
            if fill_slots and leaf.label.startswith("Extrusion"):
                region = region.convex_hull
            regions.append(region)
    return union_all(regions)


def highlight_regions(highlights, *, visible_root, assembled_root, projection):
    """Yield (Highlight, Shapely region) pairs in projected 2D millimetres.

    Seated targets use assembled geometry with the exploded view's camera.
    Masks always come from visible_root, never from assembled_root. Both models
    are supplied by the caller; this function does not build them. The mask
    cache is local to this one camera and figure.
    """
    masks = {}
    for highlight in highlights:
        target = assembled_root if highlight.assembled else visible_root
        region = union_all(
            [
                feature_region(part, projection, highlight.feature)
                for part in select_parts(target, highlight.part)
            ]
        )
        if highlight.occluded_by:
            key = highlight.occluded_by, highlight.fill_occluder_slots
            if key not in masks:
                masks[key] = occlusion_region(
                    visible_root,
                    projection,
                    highlight.occluded_by,
                    fill_slots=highlight.fill_occluder_slots,
                )
            region = region.difference(masks[key])
        if not region.is_empty:
            yield highlight, region
