"""Square nut — fastener reference shape.

Renders a single M3 square nut on its own as a hardware reference icon
for the manual's HARDWARE — REFERENCE page (page 4).

An exhibit, not an install step — no install motion, so the exploded
and assembled variants are identical; ``__main__`` renders only the
assembled one (cf. belt_11_clamp_show).

Run from the repo root:

    uv run --group cad python -m hardware.assembly.procedures.fastener_20_square_nut
"""

from build123d import Compound

from hardware.assembly.base import BaseAssembly
from hardware.assembly.projection import ISO, Camera
from hardware.parts.standard.nut import Nut


class FA20SquareNut(BaseAssembly):
    camera = [ISO, Camera(96.55, 39.21, -120.96)]

    def _build(self) -> Compound:
        nut = Nut("square", "M3").build()
        return Compound(label="fastener_20_square_nut", children=[nut])


if __name__ == "__main__":
    asm = FA20SquareNut(exploded=False)
    asm.export()
    asm.render()
