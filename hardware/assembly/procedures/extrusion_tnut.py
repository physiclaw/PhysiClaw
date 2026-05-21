"""2040 extrusion + standard T-nut — slide-in nut seated in the +Y slot
of the right 2020 cell.

The extrusion exposes the slot as a Z-axis LinearJoint; the T-nut clamps
to it via its engagement-feature RigidJoint. Slider position = the Z
coordinate of the nut's midpoint along the slot.

Run from the repo root:

    uv run --group cad python -m hardware.assembly.procedures.extrusion_tnut
"""

from build123d import Compound

from hardware.assembly.base import BaseAssembly
from hardware.assembly.render import Camera
from hardware.parts.standard.extrusion import Extrusion2040
from hardware.parts.standard.t_nut import TNut

EXTRUSION_LENGTH = 50  # mm — short stock for a compact rendering


class ExtrusionTnut(BaseAssembly):
    camera = Camera(-45, -20, 70)

    def _build(self) -> Compound:
        extrusion = Extrusion2040(length=EXTRUSION_LENGTH).build()
        tnut = TNut("standard", "M5").build()
        extrusion.joints["slot_right"].connect_to(
            tnut.joints["slot_mount"],
            position=EXTRUSION_LENGTH / 2,
        )
        return Compound(label="extrusion_tnut", children=[extrusion, tnut])


if __name__ == "__main__":
    asm = ExtrusionTnut()
    asm.export()
    asm.render()
