"""Extrusion + T-nut assembly step — exploded view of T-nut preload
into 2040 extrusions. Drawn as two layers:

  * Solid:  the real parts you hold — extrusions + loose T-nuts
            queued past the +Z end, ready to slide in.
  * Ghost (dashed): the destination positions inside each slot, where
            each nut will end up after sliding in. Nut isn't actually
            there yet — the dashed silhouette is a target marker.

  * 2 x 335 mm extrusion, each with 2 T-nuts (4 prep + 4 destinations)
  * 1 x 170 mm extrusion with 4 T-nuts (4 prep + 4 destinations)
  * 1 x 170 mm extrusion, plain

Run from the repo root:

    uv run --group cad python -m hardware.assembly.procedures.extrusion_tnut
"""

from build123d import Compound, Location

from hardware.assembly.base import GHOST_LABEL, SOLID_LABEL, BaseAssembly
from hardware.assembly.render import Camera
from hardware.parts.standard.extrusion import Extrusion2040
from hardware.parts.standard.t_nut import LENGTHS as TNUT_LENGTHS, TNut

ROW_SPACING      = 60    # mm — between extrusion centerlines along Y
PREP_START_GAP   = 5     # mm — clearance between extrusion +Z end and first prep nut
PREP_PITCH       = 15    # mm — Z pitch between adjacent prep nuts in the queue


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


def _ext_with_nuts(length: float, n_nuts: int, cb: bool = False) -> tuple:
    """Build one 2040 + two T-nut sets. Returns (ext, destinations, prep):
    * ext            — the extrusion itself
    * destinations   — N nuts seated inside the +X slot, ghost-rendered
                       as target markers
    * prep           — N loose nuts queued past the +Z end, solid"""
    ext = Extrusion2040(length=length, cb=cb).build()
    positions = _even_positions(length, n_nuts)
    destinations = _seat_nuts(ext, positions)
    prep = _seat_nuts(ext, positions)
    half_len = TNUT_LENGTHS["standard"] / 2
    for i, (nut, pos) in enumerate(zip(prep, positions)):
        target_z = length + PREP_START_GAP + i * PREP_PITCH
        seated_z = pos - half_len
        nut.move(Location((0, 0, target_z - seated_z)))
    return ext, destinations, prep


class ExtrusionTnut(BaseAssembly):
    camera = Camera(120, -20, 90)

    def _build(self) -> Compound:
        specs = [
            (335, 2, True),
            (335, 2, True),
            (170, 4, False),
            (170, 0, False),
        ]
        solid_shapes: list = []    # extrusion + loose prep nuts
        ghost_shapes: list = []    # destination silhouettes inside the slot
        for i, (length, n_nuts, cb) in enumerate(specs):
            ext, destinations, prep = _ext_with_nuts(length, n_nuts, cb=cb)
            # Bake row offset into each leaf — the solid/ghost wrappers
            # below sit at identity, so projection treats leaf locations
            # as world positions and no nested-parent ambiguity arises.
            row_offset = Location((0, i * ROW_SPACING, 0))
            for s in (ext, *destinations, *prep):
                s.move(row_offset)
            solid_shapes.append(ext)
            solid_shapes.extend(prep)
            ghost_shapes.extend(destinations)
        return Compound(label="extrusion_tnut", children=[
            Compound(label=SOLID_LABEL, children=solid_shapes),
            Compound(label=GHOST_LABEL, children=ghost_shapes),
        ])


if __name__ == "__main__":
    asm = ExtrusionTnut()
    asm.export()
    asm.render()
