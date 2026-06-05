"""Drive pulley — fastener reference shape.

Renders a single GT2 20-tooth drive pulley on its own as a hardware
reference icon for the manual's HARDWARE — REFERENCE page (page 4).

An exhibit, not an install step — no install motion, so the exploded
and assembled variants are identical; ``__main__`` renders only the
assembled one (cf. belt_11_clamp_show).

Run from the repo root:

    uv run --group cad python -m hardware.assembly.procedures.fastener_30_pulley
"""

from build123d import Compound

from hardware.assembly.base import BaseAssembly
from hardware.assembly.projection import ISO, Camera
from hardware.parts.standard.pulley import Pulley2GT20T


class FA30Pulley(BaseAssembly):
    camera = [ISO, Camera(96.55, 39.21, -120.96)]

    def _build(self) -> Compound:
        pulley = Pulley2GT20T(kind="pulley").build()
        return Compound(label="fastener_30_pulley", children=[pulley])


if __name__ == "__main__":
    asm = FA30Pulley(exploded=False)
    asm.export()
    asm.render()
