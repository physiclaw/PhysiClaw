"""Solenoid + tip — stylus tip pressed onto the solenoid's bottom rod.

The rod's lower 4 mm is grooved (thread-like) and grips the tip's M3
hole bore until the rod tip bottoms in the hole.

Run from the repo root:

    uv run --group cad python -m hardware.assembly.procedures.solenoid_tip
"""

from build123d import Compound

from hardware.assembly.base import BaseAssembly
from hardware.assembly.render import Camera
from hardware.parts.standard.solenoid import Solenoid
from hardware.parts.standard.tip import Tip


class SolenoidTip(BaseAssembly):
    camera = Camera(-45, -20, 70)

    def _build(self) -> Compound:
        solenoid = Solenoid().build()
        tip = Tip().build()
        solenoid.joints["tip_mount"].connect_to(tip.joints["solenoid_mount"])
        return Compound(label="solenoid_tip", children=[solenoid, tip])


if __name__ == "__main__":
    SolenoidTip().render()
