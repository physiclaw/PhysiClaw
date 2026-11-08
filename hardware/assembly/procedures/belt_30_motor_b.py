"""Motor B belt on the assembled stage — extends belt_20_clamp by
routing the RIGHT motor's GT2 belt across all of its pulleys and idlers.

Motor B's idler route is the world-X mirror of motor A's route shifted
to the UPPER belt plane (Y = -51.75). Its ends are seated in the upper
clamp pocket; both belts remain separate. See ``motor_path`` in
hardware.assembly.belt_routing for the mirror+shift convention and the
LJ2↔RJ2 / RJ1↔LJ1 / LU.down2↔RU.top1 / RU.down2↔LU.top1 mapping.

The base (belt_20_clamp) is always shown assembled — motor A's belt,
the X-rail slider, and the belt clamp are all already in place from
that step. Only motor B's belt has an exploded variant: lifted along
world -Y so its routing reads cleanly above the assembled stack. Its clamp
folds use the same 0.2 mm edge gap and seamless approach as motor A's
clamp illustration, with 15.8 mm downward return tails.

Run from the repo root:

    uv run --group cad python -m hardware step belt_30_motor_b
"""

from build123d import Compound, Location

from hardware.assembly.base import ASSEMBLED_CAM0, EXPLODED_CAM0, BaseAssembly
from hardware.assembly.belt_routing import motor_path
from hardware.assembly.procedures.belt_20_clamp import BE20Clamp
from hardware.assembly.projection import FRONT
from hardware.parts.standard.belt import Belt

BELT_EXPLODE = 30  # mm — exploded: shift motor B's belt further outboard
#      along world -Y so the routing reads clearly
#      above the existing structure


class BE30MotorB(BaseAssembly):
    camera = FRONT
    views = [EXPLODED_CAM0, ASSEMBLED_CAM0]

    def _build(self) -> Compound:
        base_compound = BE20Clamp(exploded=False).build()
        belt = Belt(
            path=motor_path("B", illustrate_clamp=self.exploded), name="motor_b"
        ).build()
        if self.exploded:
            belt.move(Location((0, -BELT_EXPLODE, 0)))
        return Compound(
            label="belt_30_motor_b",
            children=[
                base_compound,
                belt,
            ],
        )
