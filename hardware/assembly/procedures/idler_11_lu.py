"""Idler-mount bracket (left-up) — flat bracket tying the PulleyMountMotor
lu block to a frame extrusion via two M5×10 BHCS. One screw catches a
hammer T-nut in the frame slot; the other threads into the captive M5
square nut that idler_10_lu already seated in the lu block's top pocket.

Hole pair (along bracket local X, 20 mm apart):
  * LEFT  — frame side: BHCS M5 × 10 + hammer T-nut M5 (captured in
            the frame extrusion slot, not modelled here).
  * RIGHT — block side: BHCS M5 × 10, no nut in this sub-assembly —
            its mate is the captive top-pocket square nut from
            idler_10_lu.

Both variants follow frame_30_bracket_tnut's bracket convention so
this reads as the "block-tying twin" of that step:

  * exploded — chain along world +Z: t-nut at the bottom (LEFT hole
               only), bracket above with BRACKET_GAP of air, both
               screws above with SCREW_GAP air below their shank tips.
               The RIGHT shank tip shares the LEFT's line so the two
               heads are level.
  * assembled — loose-mate: both BHCS dropped through the bracket
                (underheads on bracket top); the LEFT shank carries
                the t-nut hanging from its tip (boss top touching the
                tip, then slid up by TNUT_LIFT to read as
                "threaded most of the way in"). The RIGHT shank just
                protrudes — its mate lives on the block side.

  * 1 x FlatBracket (40 × 18 × 4 mm, two M5 through-holes 20 mm apart)
  * 2 x BHCS M5 × 10 — driven down through the bracket
  * 1 x TNut "hammer" M5 — LEFT hole only (frame-slot side)

Run from the repo root:

    uv run --group cad python -m hardware.assembly.procedures.idler_11_lu
"""

from build123d import Compound, Location, Plane

from hardware.assembly.base import BaseAssembly
from hardware.assembly.projection import Camera
from hardware.parts.standard.bracket import (
    FlatBracket,
    flat_hole_spacing as hole_spacing,
    flat_plate_thick as plate_thick,
)
from hardware.parts.standard.screw import Screw
from hardware.parts.standard.t_nut import (
    HAMMER_TOTAL_HEIGHT,
    LENGTHS as TNUT_LENGTHS,
    TNut,
)

BHCS_LENGTH = 10     # mm — BHCS M5 underhead length
BRACKET_GAP = 20     # mm — exploded: visual gap between t-nut top and bracket bottom
SCREW_GAP   = 8      # mm — exploded: visual gap between bracket top and screw shank tips
TNUT_LIFT   = 5      # mm — assembled: nut slid up the shank toward the bracket
                     #      (closes the 6 mm shank-protrusion gap to ~1 mm,
                     #      reads as "screw threaded most of the way in")


class ID11Lu(BaseAssembly):
    # Subclasses share this build logic and only override
    # ``compound_label`` (e.g. ID21Ru reuses the same geometry for the
    # right-up corner). ``_module_stem()`` already derives the STEP/SVG
    # filename from the subclass's own module, so no other override is
    # needed.
    compound_label: str = "idler_11_lu"
    camera = Camera(15, 45, 10)

    def _build(self) -> Compound:
        bracket = FlatBracket().build()
        nut = TNut("hammer", "M5").build()
        screws = [Screw("BHCS", "M5", BHCS_LENGTH).build() for _ in range(2)]

        half_hole = hole_spacing / 2
        hammer_half_length = TNUT_LENGTHS["hammer"] / 2

        # T-nut sits under the LEFT hole only (frame-slot side). Same
        # orientation as the LEFT nut in frame_30_bracket_tnut: local +Y
        # → world +Z (boss up, bore vertical, aligned with the screw
        # shank dropping from above), slide axis along world -Y. In
        # assembled mode the nut is slid TNUT_LIFT up the shank toward
        # the bracket; exploded keeps it on the floor at z=0.
        tnut_lift = 0 if self.exploded else TNUT_LIFT
        nut.move(Location(Plane(
            origin=(-half_hole, hammer_half_length, tnut_lift),
            x_dir=(1, 0, 0),
            z_dir=(0, -1, 0),
        )))

        # Layout per variant (mirrors frame_30_bracket_tnut so the two
        # files read identically apart from the missing RIGHT t-nut):
        #   exploded:  shank tips float SCREW_GAP above the bracket top;
        #              bracket sits BRACKET_GAP above the t-nut top.
        #   assembled: BHCS dropped through the bracket with the LEFT
        #              t-nut hanging from its shank tip — bracket
        #              floats above the t-nut by (BHCS_LENGTH − plate_thick).
        if self.exploded:
            bracket_z = HAMMER_TOTAL_HEIGHT + BRACKET_GAP + plate_thick / 2
            screw_z   = bracket_z + plate_thick / 2 + SCREW_GAP + BHCS_LENGTH
        else:
            screw_z   = HAMMER_TOTAL_HEIGHT + BHCS_LENGTH
            bracket_z = screw_z - plate_thick / 2

        bracket.move(Location((0, 0, bracket_z)))
        for screw, side in zip(screws, (-half_hole, half_hole)):
            screw.move(Location((side, 0, screw_z)))

        # Expose the bracket-bottom height so a host assembly can
        # flush-mount this sub-assembly — same hook as FR30BracketTnut.
        self.bracket_bottom_z = bracket_z - plate_thick / 2

        return Compound(label=self.compound_label, children=[
            bracket, nut, *screws,
        ])


if __name__ == "__main__":
    for exploded in (True, False):
        asm = ID11Lu(exploded=exploded)
        asm.export()
        asm.render()
