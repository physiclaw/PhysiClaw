"""Motor A belt on the assembled stage — extends linear_47_idler_rj2 by
routing the LEFT motor's GT2 belt across all of its pulleys and idlers.

The belt is one continuous strip, both ends clamped on the X-carriage.
Each end follows the clamp's diagonal pocket, bends downward around its
post, and returns into the entry slot with meshed teeth. The route is:

    left clamp return → left clamp entry
        → LJ2     (X-gantry LEFT joint, lower idler — toothed)
        → LD      (front-LEFT corner idler)
        → LU.down2 (LU 1-idler stack)
        → MOTOR_A pulley
        → LU.down1 (LU 2-stack, BOTTOM idler)
        → RU.down2 (RU 1-idler stack)
        → RJ1     (X-gantry RIGHT joint, lower idler — smooth)
    → right clamp entry → right clamp return

The idler waypoints sit at world Y = -42.75 (the lower belt plane),
with a small Y offset at the carriage to center the belt in the clamp's
lower pocket and clear the slider. The motor pulley Y is set by the
LEFT_PULLEY_GAP constant in motor_30_pulley; coplanarity is part of
that assembly's calibration.

The base (linear_47_idler_rj2) stays assembled. The exploded manual view
lifts the belt outboard and opens its return folds with a 0.2 mm edge gap
so the highlighted incoming runs and 15.8 mm tails remain distinct.

Run from the repo root:

    uv run --group cad python -m hardware step belt_10_motor_a
"""

from build123d import Compound, Location

from hardware.assembly.base import EXPLODED_CAM0, BaseAssembly
from hardware.assembly.belt_routing import motor_path
from hardware.assembly.procedures.linear_47_idler_rj2 import LI47IdlerRj2
from hardware.assembly.projection import FRONT
from hardware.parts.standard.belt import Belt

BELT_EXPLODE = 30  # mm — exploded: shift the belt outboard along world -Y
#      so it lifts clear of the pulleys / idlers and the
#      routing reads cleanly from the camera


class BE10MotorA(BaseAssembly):
    camera = FRONT
    views = [EXPLODED_CAM0]

    def __init__(self, *, exploded=False, illustrate_clamp=False):
        super().__init__(exploded=exploded)
        # BELT_10 opens the folds in its exploded view. BELT_20 can request
        # the same spacing while keeping this entire assembly seated.
        self.illustrate_clamp = exploded or illustrate_clamp

    def _build(self) -> Compound:
        base_compound = LI47IdlerRj2(exploded=False).build()
        belt = Belt(
            path=motor_path("A", illustrate_clamp=self.illustrate_clamp), name="motor_a"
        ).build()
        if self.exploded:
            belt.move(Location((0, -BELT_EXPLODE, 0)))
        return Compound(
            label="belt_10_motor_a",
            children=[
                base_compound,
                belt,
            ],
        )
