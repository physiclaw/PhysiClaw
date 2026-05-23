"""Idler mount (right-down) — single smooth idler raised on an M5×10×9
spacer, held to a PulleyMountFront block by a 20 mm M4 shoulder screw
through the block's top-face M4 hole (at world x = 0, y = top_hole_y,
on the +Y-half of the top face). The 5 mm shoulder seats on the block
top; the M4 thread reaches down into the 4.3 mm clearance hole and
threads into the captive M4 square nut sitting in the +Y-face back
pocket (bore vertical, aligned with the through-hole).

Stack, in the user's top → bottom order:
  idler (smooth) / ring M5×8×0.5 / ring M5×10×9
  Stack = 8.5 + 0.5 + 9 = 18 mm; shoulder 20 mm → 2 mm axial play.

Two variants:
  * exploded — stack laid out along +Z with EXPLODE_SEPARATION of air
               between adjacent parts; thread tip floats SCREW_GAP
               above the stack top (same shank-tip convention used by
               idler_10_lu / motor_10_bracket). The square nut pulls
               outward NUT_GAP along +Y, the back-face install axis.
  * assembled — stack tight on block top; nut bore vertical, centered
                in the back pocket and aligned with the screw thread.

The block's stadium slot (with its M5 through-hole) is the
frame-mount interface — not used by this sub-assembly.

Run from the repo root:

    uv run --group cad python -m hardware.assembly.procedures.idler_40_rd
"""

from build123d import Axis, Compound, Location

from hardware.assembly.base import BaseAssembly
from hardware.assembly.render import Camera
from hardware.parts.custom.pulley_mount_front import (
    PulleyMountFront,
    back_pocket_center_z,
    thickness as block_thickness,
    top_hole_y,
    width as block_width,
)
from hardware.parts.standard.nut import SPECS as NUT_SPECS, Nut
from hardware.parts.standard.pulley import Pulley2GT20T, flange_belt_h
from hardware.parts.standard.ring import SPECS as RING_SPECS, Ring
from hardware.parts.standard.screw import SHOULDER_DIMS, Screw

WASHER_SPEC        = "M5x8x0.5"
SPACER_SPEC        = "M5x10x9"
SHOULDER_LEN       = 20    # mm — covers spacer + washer + idler (18 mm), 2 mm play
EXPLODE_SEPARATION =  5    # mm — exploded: air between adjacent parts in the stack
SCREW_GAP          = 12    # mm — exploded: stack top → screw thread tip
NUT_GAP            = 15    # mm — exploded: back face → nut center along +Y install axis


class ID40Rd(BaseAssembly):
    camera = Camera(-30, 25)

    def _build(self) -> Compound:
        block = PulleyMountFront().build()
        washer_h      = RING_SPECS[WASHER_SPEC]["height"]
        spacer_h      = RING_SPECS[SPACER_SPEC]["height"]
        nut_thickness = NUT_SPECS["square"]["M4"]["thickness"]
        block_top_z   = block_thickness / 2
        back_face_y   = block_width / 2
        thread_len    = SHOULDER_DIMS["M4"]["thread_len"]

        # Stack on the screw axis (x=0, y=top_hole_y), bottom → top.
        stack = [
            (Ring(SPACER_SPEC).build(),                         spacer_h),
            (Ring(WASHER_SPEC).build(),                         washer_h),
            (Pulley2GT20T(kind="idler", toothed=False).build(), flange_belt_h),
        ]

        sep = EXPLODE_SEPARATION if self.exploded else 0

        placed = []
        cursor_z = block_top_z + sep
        for part, h in stack:
            part.move(Location((0, top_hole_y, cursor_z)))
            placed.append(part)
            cursor_z += h + sep
        stack_top_z = cursor_z - sep

        # Shoulder screw: shoulder bottom seats on the block top face in
        # assembled mode. Exploded: thread tip floats SCREW_GAP above the
        # stack top (same convention as motor_10_bracket / idler_10_lu).
        if self.exploded:
            underhead_z = stack_top_z + SCREW_GAP + thread_len + SHOULDER_LEN
        else:
            underhead_z = block_top_z + SHOULDER_LEN
        screw = Screw("SHOULDER", "M4", SHOULDER_LEN).build()
        screw.move(Location((0, top_hole_y, underhead_z)))
        placed.append(screw)

        # Captive square nut in the +Y back pocket, flipped chamfer-down so
        # the chamfer eases the lead-in for the screw thread approaching
        # from above. Exploded: pulled outward NUT_GAP along +Y (the back
        # face it slides through).
        nut = Nut("square", "M4").build().rotate(Axis.X, 180)
        nut_y = back_face_y + NUT_GAP if self.exploded else top_hole_y
        nut_bottom_z = back_pocket_center_z - nut_thickness / 2
        # After the flip the part spans local z ∈ [-thickness, 0]; offset by
        # +thickness so the chamfered face lands at the pocket floor.
        nut.move(Location((0, nut_y, nut_bottom_z + nut_thickness)))
        placed.append(nut)

        return Compound(label="idler_40_rd", children=[block, *placed])


if __name__ == "__main__":
    for exploded in (True, False):
        asm = ID40Rd(exploded=exploded)
        asm.export()
        asm.render()
