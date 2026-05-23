"""Idler mount (right-up) — two shoulder-screw stacks fastened into the
PulleyMountMotor block's two outer M4 through-holes (top face, at
x = ±outer_hole_offset). Both columns use a 20 mm shoulder: the LEFT
column raises a single idler on an M5×10×9 spacer with a thin thrust
washer between them, the RIGHT column stacks two idlers each on its
own thin washer. M4 square nuts captured in the block's left/right
side pockets engage each shoulder screw's M4 thread.

Per stack: the 5 mm shoulder seats on the block's top face; the M4
thread reaches down into the 4.3 mm clearance hole and threads into
the captive square nut sitting in the side pocket (bore vertical,
aligned with the through-hole).

Stacks, in the user's top → bottom order:
  * LEFT (x = -outer_hole_offset), SHOULDER M4 × 20 mm
    idler (smooth) / ring M5×8×0.5 / ring M5×10×9
    Stack = 8.5 + 0.5 + 9 = 18 mm; shoulder 20 mm → 2 mm axial play.
  * RIGHT (x = +outer_hole_offset), SHOULDER M4 × 20 mm
    idler (smooth) / ring M5×8×0.5 / idler (smooth) / ring M5×8×0.5
    Stack = 2 × 8.5 + 2 × 0.5 = 18 mm; shoulder 20 mm → 2 mm play.

Each column also captures one M4 square nut in the block's side
pocket opening on the same side (LEFT nut in the -X-face pocket,
RIGHT nut in the +X-face pocket).

Two variants:
  * exploded — each stack laid out along +Z with EXPLODE_SEPARATION of
               air between adjacent parts. Both screws share a single
               shank-tip line (SCREW_GAP above the taller stack) so
               their thread tips align across columns; with equal
               shoulder lengths the heads also end up at the same Z.
               Same convention as motor_10_bracket. The square nuts pull
               outward by NUT_GAP along their ±X install axis (the
               side face they slide through).
  * assembled — stack tight on block top; nut bore vertical, centered
                in its pocket and aligned with the screw thread.

The center M5 counterbore and the -Y face M5 hole are the block's
frame-mount interface and are not used by this sub-assembly.

Run from the repo root:

    uv run --group cad python -m hardware.assembly.procedures.idler_20_ru
"""

from build123d import Axis, Compound, Location

from hardware.assembly.base import BaseAssembly
from hardware.assembly.render import Camera
from hardware.parts.custom.pulley_mount_motor import (
    PulleyMountMotor,
    length as block_length,
    outer_hole_offset,
    side_pocket_center_z,
    thickness as block_thickness,
)
from hardware.parts.standard.nut import SPECS as NUT_SPECS, Nut
from hardware.parts.standard.pulley import Pulley2GT20T, flange_belt_h
from hardware.parts.standard.ring import SPECS as RING_SPECS, Ring
from hardware.parts.standard.screw import SHOULDER_DIMS, Screw

WASHER_SPEC        = "M5x8x0.5"
SPACER_SPEC        = "M5x10x9"
LEFT_SHOULDER_LEN  = 20    # mm — covers spacer + washer + idler (18 mm)
RIGHT_SHOULDER_LEN = 20    # mm — covers washer + idler + washer + idler (18 mm)
EXPLODE_SEPARATION =  5    # mm — exploded: air between adjacent parts in a stack
SCREW_GAP          = 12    # mm — exploded: taller-stack top → shared shank-tip line
NUT_GAP            = 15    # mm — exploded: block side face → nut center, along install axis


class ID20Ru(BaseAssembly):
    camera = Camera(-30, 25)

    def _build(self) -> Compound:
        block = PulleyMountMotor().build()
        washer_h      = RING_SPECS[WASHER_SPEC]["height"]
        spacer_h      = RING_SPECS[SPACER_SPEC]["height"]
        nut_thickness = NUT_SPECS["square"]["M4"]["thickness"]
        block_top_z   = block_thickness / 2
        block_side_x  = block_length / 2
        thread_len    = SHOULDER_DIMS["M4"]["thread_len"]

        idler  = lambda: Pulley2GT20T(kind="idler", toothed=False).build()
        washer = lambda: Ring(WASHER_SPEC).build()
        spacer = lambda: Ring(SPACER_SPEC).build()
        columns = [
            (-outer_hole_offset, LEFT_SHOULDER_LEN, [
                (spacer, spacer_h),
                (washer, washer_h),
                (idler,  flange_belt_h),
            ]),
            (+outer_hole_offset, RIGHT_SHOULDER_LEN, [
                (washer, washer_h),
                (idler,  flange_belt_h),
                (washer, washer_h),
                (idler,  flange_belt_h),
            ]),
        ]

        sep = EXPLODE_SEPARATION if self.exploded else 0

        placed = []
        column_tops = []
        for x, _, stack in columns:
            cursor_z = block_top_z + sep
            for factory, h in stack:
                p = factory()
                p.move(Location((x, 0, cursor_z)))
                placed.append(p)
                cursor_z += h + sep
            column_tops.append(cursor_z - sep)

        # Shared shank-tip line in exploded mode (motor_10_bracket convention):
        # both screws' thread tips align at thread_tip_z, so heads end up at
        # heights differing only by (LEFT - RIGHT) shoulder length.
        if self.exploded:
            thread_tip_z = max(column_tops) + SCREW_GAP

        for x, shoulder_len, _ in columns:
            screw = Screw("SHOULDER", "M4", shoulder_len).build()
            if self.exploded:
                underhead_z = thread_tip_z + thread_len + shoulder_len
            else:
                underhead_z = block_top_z + shoulder_len
            screw.move(Location((x, 0, underhead_z)))
            placed.append(screw)

        # Captive square nut per side pocket, flipped chamfer-down so the
        # chamfer eases the lead-in for the screw thread approaching from
        # above. Exploded: nut pulled outward NUT_GAP along its ±X install
        # axis (the side face it slides through).
        nut_bottom_z = side_pocket_center_z - nut_thickness / 2
        for x, _, _ in columns:
            nut = Nut("square", "M4").build().rotate(Axis.X, 180)
            side_sign = 1 if x > 0 else -1
            nut_x = (
                side_sign * (block_side_x + NUT_GAP) if self.exploded else x
            )
            # After the flip the part spans local z ∈ [-thickness, 0]; offset
            # by +thickness so the chamfered face lands at the pocket floor.
            nut.move(Location((nut_x, 0, nut_bottom_z + nut_thickness)))
            placed.append(nut)

        return Compound(label="idler_20_ru", children=[block, *placed])


if __name__ == "__main__":
    for exploded in (True, False):
        asm = ID20Ru(exploded=exploded)
        asm.export()
        asm.render()
