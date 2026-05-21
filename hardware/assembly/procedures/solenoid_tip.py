"""Solenoid + tip — stylus tip pressed onto the solenoid's bottom rod.
Drawn as two layers:

  * Solid: solenoid + tip in its prep position, dropped along -Z below
           the rod. The tip slides UP (+Z) onto the rod to install.
  * Ghost (dashed): the tip at its seated destination on the rod tip.

The rod's lower 4 mm is grooved (thread-like) and grips the tip's M3
hole bore until the rod tip bottoms in the hole.

Run from the repo root:

    uv run --group cad python -m hardware.assembly.procedures.solenoid_tip
"""

from build123d import Compound, Location

from hardware.assembly.base import GHOST_LABEL, SOLID_LABEL, BaseAssembly
from hardware.assembly.render import Camera
from hardware.parts.standard.solenoid import Solenoid
from hardware.parts.standard.tip import Tip

PREP_OFFSET_Z = -25   # mm — prep tip dropped below the seated position


class SolenoidTip(BaseAssembly):
    camera = Camera(-45, -20, 70)

    def _build(self) -> Compound:
        solenoid = Solenoid().build()
        destination = Tip().build()
        prep = Tip().build()
        # Both tips connect to the same solenoid joint and land at the
        # seated location; drop prep along -Z to show it mid-insertion.
        solenoid.joints["tip_mount"].connect_to(destination.joints["solenoid_mount"])
        solenoid.joints["tip_mount"].connect_to(prep.joints["solenoid_mount"])
        prep.move(Location((0, 0, PREP_OFFSET_Z)))
        return Compound(label="solenoid_tip", children=[
            Compound(label=SOLID_LABEL, children=[solenoid, prep]),
            Compound(label=GHOST_LABEL, children=[destination]),
        ])


if __name__ == "__main__":
    asm = SolenoidTip()
    asm.export()
    asm.render()
