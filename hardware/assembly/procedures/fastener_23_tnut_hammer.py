"""Hammer T-nut — fastener reference shape.

Renders a single 2020 hammer (drop-and-twist) T-slot nut on its own as
a hardware reference icon for the manual's HARDWARE — REFERENCE page
(page 4).

An exhibit, not an install step — no install motion, so the exploded
and assembled variants are identical; ``__main__`` renders only the
assembled one (cf. belt_11_clamp_show).

Run from the repo root:

    uv run --group cad python -m hardware.assembly.procedures.fastener_23_tnut_hammer
"""

from build123d import Compound

from hardware.assembly.base import BaseAssembly
from hardware.assembly.projection import ISO, Camera
from hardware.parts.standard.t_nut import TNut


class FA23TNutHammer(BaseAssembly):
    camera = [ISO, Camera(167.79, -34.56, -36.49)]

    def _build(self) -> Compound:
        tnut = TNut("hammer", "M5").build()
        return Compound(label="fastener_23_tnut_hammer", children=[tnut])


if __name__ == "__main__":
    asm = FA23TNutHammer(exploded=False)
    asm.export()
    asm.render()
