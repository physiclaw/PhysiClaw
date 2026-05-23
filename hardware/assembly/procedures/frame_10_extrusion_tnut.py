"""Extrusion + T-nut preload — slide standard T-nuts into 2040 slots.

The two variants use intentionally different layouts:

  * exploded — four extrusions stacked in Y rows.
      Solid:  extrusion + loose T-nuts queued past its +Z end, ready
              to slide in.
      Ghost:  destination silhouettes inside the slot — the seat
              positions the loose nuts will end up at.
      Each row's prep queue extends along its OWN +Z direction with no
      neighbor in the way; the install motion (slide along +Z into
      slot) reads cleanly per row. Row layout is intentional — easier
      to follow than a frame layout where prep queues from different
      members would collide.

  * assembled — the four members arranged as a rectangular frame:
                longs flush against shorts (separation=0), counterbore
                faces outboard, each long CB axis collinear with the
                matching short's end-cell bore. Nuts seated at their
                destinations; single layer.

  * 2 x 335 mm long extrusion (cb), each with 2 standard M5 T-nuts
  * 1 x 170 mm short extrusion (top) with 4 standard M5 T-nuts
  * 1 x 170 mm short extrusion (bottom), plain

Run from the repo root:

    uv run --group cad python -m hardware.assembly.procedures.frame_10_extrusion_tnut
"""

from build123d import Compound, Location, Plane

from hardware.assembly.base import GHOST_LABEL, SOLID_LABEL, BaseAssembly
from hardware.assembly.render import Camera
from hardware.parts.standard.extrusion import (
    Extrusion2040,
    cb_end_offset,
    leg,
)
from hardware.parts.standard.t_nut import LENGTHS as TNUT_LENGTHS, TNut

LONG_LENGTH      = 335
SHORT_LENGTH     = 170
EXT_THICKNESS    = 2 * leg   # 2040 narrow cross-section (= 20 mm)

ROW_SPACING      = 60    # mm — exploded: between extrusion centerlines along Y
PREP_START_GAP   = 5     # mm — clearance between extrusion +Z end and first prep nut
PREP_PITCH       = 15    # mm — Z pitch between adjacent prep nuts in the queue

# 4 nuts on the top short: outer pair 30 mm in from each end, inner
# pair another 25 mm inboard. Leaves a 60 mm clear span between the
# inner pair for future mid-frame mounting.
SHORT_TOP_END_GAP    = 30
SHORT_TOP_INNER_GAP  = 25
SHORT_TOP_POSITIONS = [
    SHORT_TOP_END_GAP,
    SHORT_TOP_END_GAP + SHORT_TOP_INNER_GAP,
    SHORT_LENGTH - SHORT_TOP_END_GAP - SHORT_TOP_INNER_GAP,
    SHORT_LENGTH - SHORT_TOP_END_GAP,
]


def _even_positions(length: float, n: int) -> list[float]:
    """N positions spread evenly along [0, length] with equal end gaps."""
    return [length * (2 * i + 1) / (2 * n) for i in range(n)]


def _seat_nuts(ext, positions: list[float]) -> list:
    """Build and seat one TNut per position into ext's slot_right slot."""
    nuts = [TNut("standard", "M5").build() for _ in positions]
    for nut, pos in zip(nuts, positions):
        ext.joints["slot_right"].connect_to(
            nut.joints["slot_mount"], position=pos,
        )
    return nuts


def ext_with_nuts(
    length: float,
    n_nuts: int,
    cb: bool = False,
    with_prep: bool = True,
    positions: list[float] | None = None,
) -> tuple:
    """Build one 2040 + T-nut sets. Returns (ext, destinations, prep):
    * ext            — the extrusion itself
    * destinations   — N nuts seated inside the +X slot
    * prep           — N loose nuts queued past the +Z end, solid;
                       empty list when ``with_prep=False`` so callers
                       that only want the seated state don't pay for
                       N unused TNut builds.

    ``positions`` (mm along Z) overrides the default even-spread layout
    when the caller needs custom placement (e.g. the top short's 4
    nuts use a manually-tuned spread). When supplied, ``n_nuts`` must
    match ``len(positions)``."""
    ext = Extrusion2040(length=length, cb=cb).build()
    if positions is None:
        positions = _even_positions(length, n_nuts)
    destinations = _seat_nuts(ext, positions)
    if not with_prep:
        return ext, destinations, []
    prep = _seat_nuts(ext, positions)
    half_len = TNUT_LENGTHS["standard"] / 2
    for i, (nut, pos) in enumerate(zip(prep, positions)):
        target_z = length + PREP_START_GAP + i * PREP_PITCH
        seated_z = pos - half_len
        nut.move(Location((0, 0, target_z - seated_z)))
    return ext, destinations, prep


class FR10ExtrusionTnut(BaseAssembly):

    def __init__(self, *, separation: float = 30, exploded: bool = False):
        """``separation`` (mm) — horizontal gap between each long and
        the short ends in the assembled view. Default 30 leaves the
        longs visibly apart from the shorts so the preload reads as
        "longs placed nearby but not yet joined to the shorts."
        No effect on the exploded view (which uses the row layout)."""
        super().__init__(exploded=exploded)
        self.separation = separation
        # Populated by _build_assembled_frame after .build(): keyed by
        # "long_left" / "long_right" / "short_top" / "short_bot", each
        # value is (ext, destinations). Lets a downstream assembly
        # (e.g. frame_20_SHCS) reach the longs' CB joints for placing
        # screws onto the moved members. Empty in the exploded row
        # variant — joints there are decoupled from the next step.
        self.frame_parts: dict = {}

    @property
    def camera(self) -> Camera:
        # Two layouts → two cameras. Exploded looks down the rows from
        # the side; assembled views the frame head-on from the front.
        return Camera(120, -20, 90) if self.exploded else Camera(-30, -20)

    def _build(self) -> Compound:
        return (self._build_exploded_rows() if self.exploded
                else self._build_assembled_frame())

    def _build_exploded_rows(self) -> Compound:
        specs = [
            # (length, n_nuts, cb, positions or None for even spread)
            (LONG_LENGTH,  2, True,  None),
            (LONG_LENGTH,  2, True,  None),
            (SHORT_LENGTH, 4, False, SHORT_TOP_POSITIONS),
            (SHORT_LENGTH, 0, False, None),
        ]
        solid_shapes = []    # extrusion + loose prep nuts
        ghost_shapes = []    # destination silhouettes inside the slot
        for i, (length, n_nuts, cb, positions) in enumerate(specs):
            ext, destinations, prep = ext_with_nuts(
                length, n_nuts, cb=cb, positions=positions,
            )
            # Bake row offset into each leaf — the solid/ghost wrappers
            # below sit at identity, so projection treats leaf locations
            # as world positions and no nested-parent ambiguity arises.
            row_offset = Location((0, i * ROW_SPACING, 0))
            for s in (ext, *destinations, *prep):
                s.move(row_offset)
            solid_shapes.append(ext)
            solid_shapes.extend(prep)
            ghost_shapes.extend(destinations)
        return Compound(label="frame_10_extrusion_tnut", children=[
            Compound(label=SOLID_LABEL, children=solid_shapes),
            Compound(label=GHOST_LABEL, children=ghost_shapes),
        ])

    def _build_assembled_frame(self) -> Compound:
        # self.separation pulls each long outboard from the short ends
        # (0 = flush/closed).
        #
        # All members: slot face → world -Y (toward front camera) so
        # seated nuts are visible. The longs also need their +Y (CB)
        # face outboard horizontally — chirality forces one to be
        # flipped end-to-end: long_left uses z_dir=(0,0,-1) with origin
        # at Z=LONG_LENGTH; long_right uses z_dir=(0,0,1).
        half_w = SHORT_LENGTH / 2 + EXT_THICKNESS / 2 + self.separation
        top_z  = LONG_LENGTH - cb_end_offset
        bot_z  = cb_end_offset

        members = [
            # name, (length, n_nuts, cb, positions), placement
            ("long_left",  (LONG_LENGTH,  2, True,  None),
             Plane(origin=(-half_w, 0, LONG_LENGTH),
                   x_dir=(0, -1, 0), z_dir=(0, 0, -1))),   # CB → world -X
            ("long_right", (LONG_LENGTH,  2, True,  None),
             Plane(origin=(half_w, 0, 0),
                   x_dir=(0, -1, 0), z_dir=(0, 0, 1))),    # CB → world +X
            ("short_top",  (SHORT_LENGTH, 4, False, SHORT_TOP_POSITIONS),
             Plane(origin=(-SHORT_LENGTH / 2, 0, top_z),
                   x_dir=(0, -1, 0), z_dir=(1, 0, 0))),
            ("short_bot",  (SHORT_LENGTH, 0, False, None),  # n_nuts=0: no nuts
             Plane(origin=(-SHORT_LENGTH / 2, 0, bot_z),
                   x_dir=(0, -1, 0), z_dir=(1, 0, 0))),
        ]
        shapes = []
        for name, (length, n_nuts, cb, positions), plane in members:
            ext, destinations, _ = ext_with_nuts(
                length, n_nuts, cb=cb, with_prep=False, positions=positions,
            )
            loc = Location(plane)
            for s in (ext, *destinations):
                s.move(loc)
            self.frame_parts[name] = (ext, destinations)
            shapes.append(ext)
            shapes.extend(destinations)

        return Compound(label="frame_10_extrusion_tnut", children=shapes)


if __name__ == "__main__":
    for exploded in (True, False):
        asm = FR10ExtrusionTnut(exploded=exploded)
        asm.export()
        asm.render()
