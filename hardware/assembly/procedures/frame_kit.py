"""Frame assembly step — four 2040 extrusions joined as a rectangle by
8 SHCS M6 screws. Horizontal-only explode: longs pushed outward by a
gap so each counterbore axis stays collinear with the short's end-cell
bore. Screws floated further outboard along the entry axis.

  * 2 x 335 mm long extrusions (cb, 2 seated M5 T-nuts each) — vertical
  * 1 x 170 mm short extrusion (4 seated M5 T-nuts) — top
  * 1 x 170 mm short extrusion (plain) — bottom
  * 8 x SHCS M6 — through long-extrusion counterbores, into short-end cells

Run from the repo root:

    uv run --group cad python -m hardware.assembly.procedures.frame_kit
"""

from build123d import Compound, Location, Plane

from hardware.assembly.base import BaseAssembly
from hardware.assembly.procedures.extrusion_tnut import ext_with_nuts
from hardware.assembly.render import Camera
from hardware.parts.standard.extrusion import (
    CB_LABELS,
    Extrusion2040,
    cb_end_offset,
    leg,
)
from hardware.parts.standard.screw import Screw

LONG_LENGTH    = 335
SHORT_LENGTH   = 170
EXT_THICKNESS  = 2 * leg  # 2040 narrow cross-section (= 20 mm)
FRAME_GAP      = 30       # mm — horizontal gap between long inner face and short end
SCREW_EXPLODE  = -35      # mm — negative LinearJoint position = outboard
SCREW_LENGTH   = 16       # mm — SHCS M6 underhead length


class FrameKit(BaseAssembly):
    camera = Camera(-30, -20)  # front view

    def _build(self) -> Compound:
        # All four members carry seated T-nuts where the prior step's
        # ghosts landed. with_prep=False discards the loose-queue copies.
        short_top_ext, short_top_nuts, _ = ext_with_nuts(
            SHORT_LENGTH, 4, with_prep=False,
        )
        long_left_ext, long_left_nuts, _ = ext_with_nuts(
            LONG_LENGTH, 2, cb=True, with_prep=False,
        )
        long_right_ext, long_right_nuts, _ = ext_with_nuts(
            LONG_LENGTH, 2, cb=True, with_prep=False,
        )
        short_bot = Extrusion2040(length=SHORT_LENGTH).build()

        # Vertical alignment: short Z = long CB Z, so each screw's axis is
        # collinear with the short's end-cell bore. No vertical explode —
        # only horizontal (longs pushed outward by FRAME_GAP).
        half_w = SHORT_LENGTH / 2 + EXT_THICKNESS / 2 + FRAME_GAP
        top_z  = LONG_LENGTH - cb_end_offset
        bot_z  = cb_end_offset

        # All extrusions oriented so the +X (slot) face points to world
        # -Y (toward the front camera) — seated T-nuts stay visible.
        # The longs additionally need the +Y (CB) face outboard horizontally
        # so screws explode along ±X. Chirality forces one of the two
        # longs to be flipped end-to-end along its length: long_left uses
        # z_dir=(0,0,-1) with origin at Z=LONG_LENGTH; long_right uses
        # the natural z_dir=(0,0,1).
        long_left_loc = Location(Plane(
            origin=(-half_w, 0, LONG_LENGTH),
            x_dir=(0, -1, 0),  # slot face → world -Y
            z_dir=(0,  0, -1), # y_dir = (-1, 0, 0): CB face → world -X
        ))
        for s in (long_left_ext, *long_left_nuts):
            s.move(long_left_loc)
        long_right_loc = Location(Plane(
            origin=(half_w, 0, 0),
            x_dir=(0, -1, 0),  # slot face → world -Y
            z_dir=(0,  0,  1), # y_dir = ( 1, 0, 0): CB face → world +X
        ))
        for s in (long_right_ext, *long_right_nuts):
            s.move(long_right_loc)

        def short_loc(z: float) -> Location:
            return Location(Plane(
                origin=(-SHORT_LENGTH / 2, 0, z),
                x_dir=(0, -1, 0),  # slot face → world -Y
                z_dir=(1,  0,  0), # length axis along world +X
            ))
        for s in (short_top_ext, *short_top_nuts):
            s.move(short_loc(top_z))
        short_bot.move(short_loc(bot_z))

        # 8 SHCS M6 — one per counterbore on each long, exploded outboard
        # along the CB axis. connect_to runs after the long has been
        # moved, so each screw inherits the long's world transform.
        screws = []
        for long_ext in (long_left_ext, long_right_ext):
            for cb in CB_LABELS:
                screw = Screw("SHCS", "M6", SCREW_LENGTH).build()
                long_ext.joints[cb].connect_to(
                    screw.joints["head"], position=SCREW_EXPLODE,
                )
                screws.append(screw)

        return Compound(label="frame_kit", children=[
            long_left_ext, *long_left_nuts,
            long_right_ext, *long_right_nuts,
            short_top_ext, *short_top_nuts,
            short_bot,
            *screws,
        ])


if __name__ == "__main__":
    asm = FrameKit()
    asm.export()
    asm.render()
