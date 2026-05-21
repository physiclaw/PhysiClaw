"""Bracket-to-frame fastening — 4 flat brackets, one at each frame
corner, each spanning where a short extrusion meets a long. Fastened
by 2 BHCS M5 driven into 2 hammer T-nuts (one per slot) per bracket.
Reinforces the M6 SHCS end-tap joint already in place from frame_kit.

Geometry: each bracket lies flat on the world -Y slot face, 40 mm
width straddling the corner edge (world X = ±SHORT_LENGTH/2). One
hole sits at the long's centerline (10 mm outboard of the corner
edge); the other lands over the short's slot mouth (10 mm inboard) —
matching the bracket's 20 mm hole spacing without redesign.

  * 4 x FlatBracket — top-left, top-right, bottom-left, bottom-right
                      (all on the world -Y face)
  * 8 x BHCS M5 × 10 — 2 per bracket, seated IN the bracket holes
                       (head outboard, shank protrudes inboard).
  * 8 x TNut "hammer" M5 — 2 per bracket, drop-and-twist into the
                            slot from outside. Shown in the INSERTION
                            orientation (10 mm width along the slot
                            axis, 6 mm length across the ~6.2 mm mouth)
                            so the reader sees a t-nut that actually
                            fits through the mouth. Boss top touches
                            the BHCS shank tip — about to drop in,
                            twist 90° about its bore to lock, then
                            receive the screw.
  * 4 x TNut "standard" M5 — 2 per LONG extrusion, slid in from the
                              end and parked at the inner thirds.
                              Pre-loaded for a later assembly step,
                              NOT engaged by this bracket fastening.

The bracket+BHCS subassembly is exploded as one rigid piece (screw
already inserted through the bracket hole). The hammer t-nut is
exploded separately, just inboard of the BHCS shank tip — so the
reader sees "drop the nut in the slot, then drive the screw into it."
Frame M6 SHCS are shown fully seated in the long's counterbores: the
connection being reinforced.

Run from the repo root:

    uv run --group cad python -m hardware.assembly.procedures.bracket_frame
"""

from build123d import Compound, Location, Plane

from hardware.assembly.base import BaseAssembly
from hardware.assembly.render import Camera
from hardware.parts.standard.bracket import FlatBracket, hole_spacing, plate_thick
from hardware.parts.standard.extrusion import (
    CB_LABELS,
    Extrusion2040,
    cb_end_offset,
    cb_head_depth,
    leg,
)
from hardware.parts.standard.screw import Screw
from hardware.parts.standard.t_nut import (
    HAMMER_TOTAL_HEIGHT,
    LENGTHS as TNUT_LENGTHS,
    TNut,
)

LONG_LENGTH    = 335
SHORT_LENGTH   = 170
EXT_THICKNESS  = 2 * leg     # 20 mm — narrow side of 2040 (slot face plane)
BHCS_LENGTH    = 10          # mm — bracket BHCS M5 underhead length
SHCS_LENGTH    = 16          # mm — frame SHCS M6 underhead length
HALF_HOLE      = hole_spacing / 2

# Explode along the slot-face normal (world -Y):
#   slot face
#     ↓  (derived air gap, ~8 mm)
#   hammer t-nut (4.5 mm, boss out toward bracket) — boss top
#     contacts the BHCS shank tip
#   BHCS shank tip → bracket bottom → bracket top → BHCS head
# The bracket sits BRACKET_EXPLODE outboard of the slot face. The
# BHCS is seated IN the bracket (underhead on bracket top, shank tip
# protrudes BHCS_LENGTH − plate_thick past the bracket bottom). The
# hammer t-nut boss top touches the BHCS shank tip (zero gap), which
# reads as "screw is about to thread into this nut."
BRACKET_EXPLODE  = 20.5   # slot face → bracket bottom


class BracketFrame(BaseAssembly):
    camera = Camera(-30, 25)

    def _build(self) -> Compound:
        # ── Frame in closed (assembled) state — context for the brackets ──
        # No FRAME_GAP / SCREW_EXPLODE: the prior step is shown completed
        # so the new brackets visibly reinforce an existing connection.
        half_w = SHORT_LENGTH / 2 + EXT_THICKNESS / 2
        top_z  = LONG_LENGTH - cb_end_offset
        bot_z  = cb_end_offset

        short_top  = Extrusion2040(length=SHORT_LENGTH).build()
        short_bot  = Extrusion2040(length=SHORT_LENGTH).build()
        long_left  = Extrusion2040(length=LONG_LENGTH, cb=True).build()
        long_right = Extrusion2040(length=LONG_LENGTH, cb=True).build()

        long_left.move(Location(Plane(
            origin=(-half_w, 0, LONG_LENGTH),
            x_dir=(0, -1, 0), z_dir=(0, 0, -1),
        )))
        long_right.move(Location(Plane(
            origin=(half_w, 0, 0),
            x_dir=(0, -1, 0), z_dir=(0, 0, 1),
        )))
        for short, z in ((short_top, top_z), (short_bot, bot_z)):
            short.move(Location(Plane(
                origin=(-SHORT_LENGTH / 2, 0, z),
                x_dir=(0, -1, 0), z_dir=(1, 0, 0),
            )))

        # M6 SHCS fully seated in the long's counterbores — head bottoms
        # on the CB floor, heads ~flush with the long's narrow face.
        frame_screws = []
        for long_ext in (long_left, long_right):
            for cb in CB_LABELS:
                s = Screw("SHCS", "M6", SHCS_LENGTH).build()
                long_ext.joints[cb].connect_to(
                    s.joints["head"], position=cb_head_depth,
                )
                frame_screws.append(s)

        # ── 4 standard T-nuts pre-loaded in the long extrusions for a
        # later assembly step. Parked at the inner thirds of each long,
        # well clear of the corner brackets. NOT engaged by this step.
        standard_nuts: list = []
        for long_ext in (long_left, long_right):
            for p in (LONG_LENGTH / 3, 2 * LONG_LENGTH / 3):
                nut = TNut("standard", "M5").build()
                long_ext.joints["slot_right"].connect_to(
                    nut.joints["slot_mount"], position=p,
                )
                standard_nuts.append(nut)

        # ── Bracket explode chain along world -Y (magnitudes; negated
        # for world Y below). Bracket position is fixed by
        # BRACKET_EXPLODE; the BHCS seats in the bracket hole, and the
        # hammer t-nut docks at the shank tip.
        bracket_bottom_y = EXT_THICKNESS + BRACKET_EXPLODE               # 40.5
        bracket_center_y = bracket_bottom_y + plate_thick / 2            # 42.5
        bracket_top_y    = bracket_center_y + plate_thick / 2            # 44.5
        # BHCS underhead seating sits ON the bracket's outboard face;
        # the shank tip protrudes inboard by (BHCS_LENGTH − plate_thick).
        screw_origin_y   = bracket_top_y                                 # 44.5
        shank_tip_y      = bracket_top_y - BHCS_LENGTH                   # 34.5
        # Hammer t-nut boss top contacts the BHCS shank tip; the nut
        # extends HAMMER_TOTAL_HEIGHT toward the slot from there.
        hammer_bottom_y  = shank_tip_y - HAMMER_TOTAL_HEIGHT             # 30

        hammer_half_length = TNUT_LENGTHS["hammer"] / 2

        brackets: list = []
        bracket_screws: list = []
        hammer_nuts: list = []
        for sign_x in (-1, 1):
            long_x        = sign_x * half_w                              # ±95
            corner_edge_x = sign_x * (SHORT_LENGTH / 2)                  # ±85
            short_hole_x  = corner_edge_x - sign_x * HALF_HOLE           # ±75

            for corner_z in (top_z, bot_z):
                # Bracket plate on the world -Y face. Local +X → world +X
                # (holes spread along world X). Local +Z → world -Y
                # (head face outward, away from the frame).
                bracket = FlatBracket().build()
                bracket.move(Location(Plane(
                    origin=(corner_edge_x, -bracket_center_y, corner_z),
                    x_dir=(1, 0, 0),
                    z_dir=(0, -1, 0),
                )))
                brackets.append(bracket)

                # Hammer t-nut under the long-side bracket hole, shown
                # in the INSERTION orientation: 10 mm width (local +X)
                # runs ALONG the slot axis (world ±Z); 6 mm length
                # (local +Z) runs ACROSS the slot mouth (world ±X) so
                # it fits through the ~6.2 mm mouth. After dropping in,
                # the nut twists 90° about its bore axis to lock the
                # 10 mm width behind the lips. Bore (local +Y) →
                # world -Y so the boss faces the bracket.
                long_nut = TNut("hammer", "M5").build()
                long_nut.move(Location(Plane(
                    origin=(long_x - hammer_half_length, -hammer_bottom_y, corner_z),
                    x_dir=(0, 0, 1),
                    z_dir=(1, 0, 0),
                )))
                hammer_nuts.append(long_nut)

                # Hammer t-nut under the short-side bracket hole. Same
                # insertion logic with the short's slot axis along
                # world ±X: 10 mm along world X, 6 mm across world Z.
                short_nut = TNut("hammer", "M5").build()
                short_nut.move(Location(Plane(
                    origin=(short_hole_x, -hammer_bottom_y, corner_z + hammer_half_length),
                    x_dir=(1, 0, 0),
                    z_dir=(0, 0, -1),
                )))
                hammer_nuts.append(short_nut)

                # 2 BHCS M5×10 per bracket — head outboard (world -Y),
                # shank pointing inward toward the frame.
                for hole_x in (long_x, short_hole_x):
                    screw = Screw("BHCS", "M5", BHCS_LENGTH).build()
                    screw.move(Location(Plane(
                        origin=(hole_x, -screw_origin_y, corner_z),
                        x_dir=(1, 0, 0),
                        z_dir=(0, -1, 0),
                    )))
                    bracket_screws.append(screw)

        return Compound(label="bracket_frame", children=[
            long_left, long_right, short_top, short_bot,
            *frame_screws,
            *standard_nuts,
            *hammer_nuts,
            *brackets,
            *bracket_screws,
        ])


if __name__ == "__main__":
    asm = BracketFrame(exploded=True)
    asm.export()
    asm.render()
