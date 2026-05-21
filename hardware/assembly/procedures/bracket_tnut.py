"""Bracket fastening step — one flat bracket attached by 2 M5×10 BHCS
into 2 hammer T-nuts. Exploded view stacks the three part kinds along
the screw axis (world +Z): t-nuts at the bottom, bracket above, screws
on top — so the order of operations (drop t-nuts, lay bracket, drive
screws) reads top-down at a glance.

  * 1 x FlatBracket (40 × 18 × 4 mm, two M5 through-holes 20 mm apart)
  * 2 x BHCS M5 × 10 — driven down through the bracket
  * 2 x TNut "hammer" M5 — catches the screw from below

Run from the repo root:

    uv run --group cad python -m hardware.assembly.procedures.bracket_tnut
"""

from build123d import Compound, Location, Plane

from hardware.assembly.base import BaseAssembly
from hardware.assembly.render import Camera
from hardware.parts.standard.bracket import FlatBracket, hole_spacing, plate_thick
from hardware.parts.standard.screw import Screw
from hardware.parts.standard.t_nut import LENGTHS as TNUT_LENGTHS, TNut

SCREW_LENGTH    = 10     # mm — BHCS M5 underhead length
TNUT_BOSS_TOP   = 4.5    # mm — hammer top above slot floor (1.5 plate + 3 boss)
BRACKET_GAP     = 20     # mm — visual gap between t-nut top and bracket bottom
SCREW_GAP       = 8      # mm — visual gap between bracket top and screw shank tip


class BracketTnut(BaseAssembly):
    camera = Camera(15, 45, 10)

    def _build(self) -> Compound:
        bracket = FlatBracket().build()
        nuts = [TNut("hammer", "M5").build() for _ in range(2)]
        screws = [Screw("BHCS", "M5", SCREW_LENGTH).build() for _ in range(2)]

        # T-nut local: +Y is bore/boss direction, +Z is slide length. Rotate
        # so +Y → world +Z (boss up, bore vertical) and the length runs
        # along world -Y. Shift each t-nut so its bore center (local
        # x=0, z=length/2) lands at world (±half_hole, 0, 0) — directly
        # under the matching bracket hole.
        half_hole = hole_spacing / 2
        tnut_half_length = TNUT_LENGTHS["hammer"] / 2
        for nut, side in zip(nuts, (-half_hole, half_hole)):
            nut.move(Location(Plane(
                origin=(side, tnut_half_length, 0),
                x_dir=(1, 0, 0),
                z_dir=(0, -1, 0),  # y_dir = (0, 0, 1): boss → world +Z
            )))

        bracket_z = TNUT_BOSS_TOP + BRACKET_GAP + plate_thick / 2
        bracket.move(Location((0, 0, bracket_z)))

        # Screw default: head at part +Z, shank at part -Z. Identity
        # rotation keeps head up; the shank tip lands SCREW_GAP above
        # the bracket top — a visible floating clearance, not penetration.
        screw_z = bracket_z + plate_thick / 2 + SCREW_GAP + SCREW_LENGTH
        for screw, side in zip(screws, (-half_hole, half_hole)):
            screw.move(Location((side, 0, screw_z)))

        return Compound(label="bracket_tnut", children=[
            bracket, *nuts, *screws,
        ])


if __name__ == "__main__":
    asm = BracketTnut(exploded=True)
    asm.export()
    asm.render()
