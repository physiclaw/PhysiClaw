"""Flat bracket — fastener reference shape.

Renders a single flat mounting bracket (40 × 18 × 4 mm, two M5
through-holes 20 mm apart) on its own as a hardware reference icon for
the manual's HARDWARE — REFERENCE page (page 4a).

An exhibit, not an install step — no install motion, so the exploded
and assembled variants are identical; ``__main__`` renders only the
assembled one (cf. belt_11_clamp_show).

Run from the repo root:

    uv run --group cad python -m hardware.assembly.procedures.fastener_41_flat_bracket
"""

from build123d import Compound

from hardware.assembly.base import BaseAssembly
from hardware.assembly.projection import ISO, Camera
from hardware.parts.standard.bracket import FlatBracket


class FA41FlatBracket(BaseAssembly):
    camera = [ISO, Camera(32.55, -65.32, -172.53)]

    def _build(self) -> Compound:
        bracket = FlatBracket().build()
        return Compound(label="fastener_41_flat_bracket", children=[bracket])


if __name__ == "__main__":
    asm = FA41FlatBracket(exploded=False)
    asm.export()
    asm.render()
