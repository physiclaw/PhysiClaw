"""Frame parts kit — four 2040 extrusions with standard M5 T-nuts
pre-loaded in the +X face slot, arranged as parallel rows for inventory.

  * 2 × 335 mm extrusion, each with 2 T-nuts
  * 1 × 170 mm extrusion with 4 T-nuts
  * 1 × 170 mm extrusion, plain

T-nuts are spread evenly along each extrusion's length.

Run from the repo root:

    uv run --group cad python -m hardware.assembly.procedures.frame_kit
"""

from build123d import Compound, Location

from hardware.assembly.base import BaseAssembly
from hardware.parts.standard.extrusion import Extrusion2040
from hardware.parts.standard.t_nut import TNut

ROW_SPACING = 60  # mm — between extrusion centerlines along Y


def _even_positions(length: float, n: int) -> list[float]:
    """N positions spread evenly along [0, length] with equal end gaps."""
    return [length * (2 * i + 1) / (2 * n) for i in range(n)]


def _ext_with_nuts(length: float, n_nuts: int, cb: bool = False) -> Compound:
    """A 2040 extrusion with `n_nuts` standard M5 T-nuts seated in the
    +X face slot, evenly spaced along the length."""
    ext = Extrusion2040(length=length, cb=cb).build()
    nuts = [TNut("standard", "M5").build() for _ in range(n_nuts)]
    for nut, pos in zip(nuts, _even_positions(length, n_nuts)):
        ext.joints["slot_right"].connect_to(
            nut.joints["slot_mount"], position=pos,
        )
    return Compound(
        label=f"ext_{int(length)}_x{n_nuts}",
        children=[ext, *nuts],
    )


class FrameKit(BaseAssembly):
    def _build(self) -> Compound:
        subs = [
            _ext_with_nuts(335, 2, cb=True),
            _ext_with_nuts(335, 2, cb=True),
            _ext_with_nuts(170, 4),
            _ext_with_nuts(170, 0),
        ]
        for i, sub in enumerate(subs):
            sub.move(Location((0, i * ROW_SPACING, 0)))
        return Compound(label="frame_kit", children=subs)


if __name__ == "__main__":
    asm = FrameKit()
    asm.export()
    asm.render()
