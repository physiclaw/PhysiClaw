"""XY joints on the Y carriages — extends linear_11_y by mounting
XyJointRight on the LEFT slider and XyJointLeft on the RIGHT slider
(yes, swapped), each rotated 180° about its native Z so the cutout
and slant features end up on the correct side once mounted. Each
joint's four CSK M3 through-holes align with the slider's four M3
mount holes; an M3 × 10 FHCS through each pair seats the joint to
the slider (head sunk into the CSK pocket, shank through the joint
into the slider's M3 mount).

Joint orientation (both sides — the 180° Z spin is baked into the
placement plane as x_dir = (-1, 0, 0)):
  * joint native +Z (top face, where the CSK head pockets open) →
    world -Y (outboard, where the screws are turned from).
  * joint native +X → world -X.
  * joint native +Y → world -Z (derived from z_dir × x_dir).

XyJointRight is XyJointLeft mirrored across YZ, so its hole-grid
center is at native (+9, 1) (vs the left's (-9, 1)). The placement
origin is offset from the slider mount-grid center by the joint's
in-part hole-grid center so the holes land where they need to.

Two variants:
  * exploded — joints lifted outboard along world -Y by
               JOINT_EXPLODE, AND each FHCS additionally lifted
               SCREW_EXPLODE above its CSK pocket along the joint's
               native +Z (which the placement plane maps to world
               -Y). Screws end up floating above the joint so the
               install path reads "screw drops into CSK pocket, joint
               sits on slider top".
  * assembled — joint bottom flush on the slider top face; FHCS
                head top flush with the joint top face.

The base (linear_11_y) is always shown assembled.

Run from the repo root:

    uv run --group cad python -m hardware.assembly.procedures.linear_20_joint
"""

from build123d import Compound, Location, Plane

from hardware.assembly.base import BaseAssembly
from hardware.assembly.procedures.linear_11_y import LI11Y
from hardware.assembly.render import Camera
from hardware.parts.custom.xy_joint_left import (
    XyJointLeft,
    big_csk_dx_from_extra2,
    big_csk_dy_from_extra2,
    csk_hole_from_bottom,
    csk_hole_from_left,
    csk_x_spacing,
    csk_y_spacing,
    extra_hole2_dx_from_csk,
    extra_hole2_dy_from_csk,
    length as joint_length,
    thickness as joint_thickness,
    width as joint_width,
)
from hardware.parts.custom.xy_joint_right import XyJointRight
from hardware.parts.standard.screw import FHCS_DIMS, Screw, head_skirt

FHCS_LENGTH   = 10    # mm — M3 FHCS overall length
JOINT_EXPLODE = 30    # mm — exploded: joint lifted outboard along world -Y
SCREW_EXPLODE = 25    # mm — exploded: FHCS lifted above its CSK pocket along
                      #      joint native +Z (mapped to world -Y by the
                      #      placement plane), so the install path reads clearly


def _csk_positions(mirrored: bool) -> list[tuple[float, float]]:
    """Four CSK M3 hole positions (X, Y) in joint native frame.
    ``mirrored=False`` for XyJointLeft; ``mirrored=True`` flips X for
    XyJointRight (which is XyJointLeft mirrored across YZ)."""
    csk_ll_x = -joint_length / 2 + csk_hole_from_left
    csk_ll_y = -joint_width / 2 + csk_hole_from_bottom
    csk_ur_x = csk_ll_x + csk_x_spacing
    csk_ur_y = csk_ll_y + csk_y_spacing
    positions = [
        (csk_ll_x, csk_ll_y),
        (csk_ll_x, csk_ur_y),
        (csk_ur_x, csk_ll_y),
        (csk_ur_x, csk_ur_y),
    ]
    if mirrored:
        positions = [(-x, y) for x, y in positions]
    return positions


class LI20Joint(BaseAssembly):
    camera = Camera(-30, 25)

    def _build(self) -> Compound:
        self.base = LI11Y(exploded=False)
        base_compound = self.base.build()

        # FHCS M3 head height (cone + skirt rim) — head top flush with
        # joint top face puts the underhead at joint native Z =
        # thickness/2 - head_height. Exploded mode lifts the screw
        # further along native +Z so it floats above the CSK pocket.
        fhcs_head_height = FHCS_DIMS["M3"]["k"] + head_skirt
        screw_z_seated   = joint_thickness / 2 - fhcs_head_height
        screw_z = (screw_z_seated + SCREW_EXPLODE
                   if self.exploded else screw_z_seated)

        # Big CSK position in the part-native frame of XyJointLeft:
        # offset from the upper-right small-CSK hole, derived the
        # same way xy_joint_left.py builds the cube.
        csk_ll_x = -joint_length / 2 + csk_hole_from_left
        csk_ll_y = -joint_width / 2 + csk_hole_from_bottom
        csk_ur_x = csk_ll_x + csk_x_spacing
        csk_ur_y = csk_ll_y + csk_y_spacing
        extra2_x = csk_ur_x + extra_hole2_dx_from_csk
        extra2_y = csk_ur_y + extra_hole2_dy_from_csk
        big_csk_native_left = (
            extra2_x + big_csk_dx_from_extra2,
            extra2_y + big_csk_dy_from_extra2,
        )

        joints = []
        # Hook for downstream consumers (linear_30_x): world (x, y, z)
        # of each joint's big CSK hole center — used by the crossbeam
        # to position the M5 fastener through the joint into a hammer
        # t-nut in the 1020 slot.
        self.big_csk_world_centers = []
        # LEFT slider (index 0) gets XyJointRight; RIGHT slider gets
        # XyJointLeft — the joints are swapped (and 180° spun via
        # x_dir below) so their cutout / slant features land on the
        # correct frame side once installed.
        for slider_center, joint_cls, mirrored in (
            (self.base.slider_mount_centers[0], XyJointRight, True),
            (self.base.slider_mount_centers[1], XyJointLeft,  False),
        ):
            csk_positions = _csk_positions(mirrored)
            # In-part hole-grid center — used to compute the placement
            # origin so the hole grid lands on the slider mount grid.
            grid_center_x = sum(p[0] for p in csk_positions) / 4
            grid_center_y = sum(p[1] for p in csk_positions) / 4

            joint = joint_cls().build()
            screws = [
                Screw("FHCS", "M3", FHCS_LENGTH).build() for _ in range(4)
            ]
            for screw, (px, py) in zip(screws, csk_positions):
                screw.move(Location((px, py, screw_z)))
            joint_compound = Compound(
                label=("xy_joint_right" if mirrored else "xy_joint_left"),
                children=[joint, *screws],
            )

            # Placement origin — derived so:
            #   * Joint hole grid center (part (grid_center_x,
            #     grid_center_y, 0)) lands at the slider mount-grid
            #     world (sx, _, sz).  With x_dir = (-1, 0, 0) the
            #     joint's native +X maps to world -X, so the +X-axis
            #     grid_center contribution flips sign in origin_x;
            #     same for grid_center_y → origin_z via y_dir =
            #     (0, 0, -1).
            #   * Joint bottom face (native Z = -thickness/2) lands at
            #     slider top world Y (= sy).
            sx, sy, sz = slider_center
            origin_x = sx + grid_center_x
            origin_y = sy - joint_thickness / 2
            if self.exploded:
                origin_y -= JOINT_EXPLODE
            origin_z = sz + grid_center_y

            joint_compound.move(Location(Plane(
                origin=(origin_x, origin_y, origin_z),
                x_dir=(-1, 0, 0),   # joint native +X → world -X (180° Z spin)
                z_dir=(0, -1, 0),   # joint native +Z (top) → world -Y (outboard)
            )))
            joints.append(joint_compound)

            # Big CSK center in world — mirrored joints have the CSK
            # at the X-flipped part position. With this placement
            # plane: native (a, b, c) → world (origin.x - a,
            # origin.y - c, origin.z - b). For the CSK at native Z = 0
            # (joint mid-thickness) the world Y is origin_y.
            big_csk_native_x, big_csk_native_y = big_csk_native_left
            if mirrored:
                big_csk_native_x = -big_csk_native_x
            self.big_csk_world_centers.append((
                origin_x - big_csk_native_x,
                origin_y,                   # joint mid-thickness (native Z = 0)
                origin_z - big_csk_native_y,
            ))

        return Compound(label="linear_20_joint", children=[base_compound, *joints])


if __name__ == "__main__":
    for exploded in (True, False):
        asm = LI20Joint(exploded=exploded)
        asm.export()
        asm.render()
