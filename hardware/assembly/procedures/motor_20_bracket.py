"""Motor + bracket fastening (right motor) — built like motor_10_bracket
but with ``motor_z_rotation = 0`` so the motor is NOT spun 180° about Z.
The cable-connector face therefore stays on native -Y, which lands on
world +X (RIGHT side from the top view) when the bracket is composed onto
a frame's right motor mount (mirror of motor_11_frame). The shaft's D-cut
flat stays on native +Y as well. This bracket also uses the taller 12 mm
spacer (``RING_SPEC = "M6x20x12"``) so Motor B's pulley lands on the
UPPER belt plane (Motor A uses the 8 mm spacer for the lower plane).

The build logic lives in motor_10_bracket and is reused here via
inheritance, with just ``compound_label``, ``RING_SPEC``, and
``motor_z_rotation`` retargeted. See motor_10_bracket for the full part
list, variant descriptions, and placement math.

Run from the repo root:

    uv run --group cad python -m hardware.assembly.procedures.motor_20_bracket
"""

from hardware.assembly.procedures.motor_10_bracket import MO10Bracket
from hardware.assembly.projection import FRONT_LEFT_HIGH, Camera


class MO20Bracket(MO10Bracket):
    compound_label = "motor_20_bracket"
    RING_SPEC = "M6x20x12"  # 12 mm standoff → Motor B pulley on the UPPER belt plane
    motor_z_rotation = 0    # plug on native -Y → world +X (RIGHT) when frame-mounted
    camera = [FRONT_LEFT_HIGH, Camera(-16.50, 15.07, 1.95)]


if __name__ == "__main__":
    for exploded in (True, False):
        asm = MO20Bracket(exploded=exploded)
        asm.export()
        asm.render()
