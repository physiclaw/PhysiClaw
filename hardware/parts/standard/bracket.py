from build123d import *

from hardware.parts.base import BaseStandardPart

# ── FlatBracket parameters ────────────────────────────────────────────────────
flat_plate_width   = 40 * MM    # W, along X
flat_plate_height  = 18 * MM    # H, along Y
flat_plate_thick   =  4 * MM    # T, along Z

# Two inline through-holes along W, centered in H.
flat_hole_diameter = 5.5 * MM
flat_hole_spacing  = 20  * MM   # center-to-center


# ── MotorBracket parameters ───────────────────────────────────────────────────
# NEMA 17 mounting plate. Long axis along X; the shaft pass-through is
# offset 20 mm from the LEFT (-X) edge, centered in Y. The four M3
# mounting holes sit at the corners of a 31 mm square (NEMA 17 bolt
# pattern), centered on the shaft hole.
motor_plate_length = 60   * MM    # along X
motor_plate_width  = 42   * MM    # along Y
motor_plate_thick  =  2 * MM    # along Z

motor_shaft_d        = 25 * MM    # shaft / pilot pass-through
motor_shaft_x_offset = 20 * MM    # shaft hole center, from -X (left) edge

motor_mount_hole_d = 3.3 * MM     # M3 clearance
motor_mount_pitch  = 31  * MM     # NEMA 17 square pattern, corner-to-corner

# Two M5 clearance holes inset from the +X (right) edge, aligned along
# Y with 25 mm pitch and centered top-to-bottom.
motor_m5_hole_d   = 5.5 * MM      # M5 clearance
motor_m5_x_inset  = 8  * MM      # from +X (right) edge
motor_m5_pitch    = 25  * MM      # center-to-center, along Y

motor_corner_fillet = 3 * MM      # the four Z-parallel outer corners


# ── Geometry ──────────────────────────────────────────────────────────────────
class FlatBracket(BaseStandardPart):
    def name_suffix(self) -> str:
        return f"_flat_x{self.qty}"

    def bom_key(self):
        return ("FlatBracket",)

    def _build(self):
        with BuildPart() as p:
            Box(flat_plate_width, flat_plate_height, flat_plate_thick)
            with Locations((0, 0, flat_plate_thick / 2)):
                with GridLocations(flat_hole_spacing, 0, 2, 1):
                    Hole(radius=flat_hole_diameter / 2)
        return p.part


class MotorBracket(BaseStandardPart):
    """NEMA 17 motor mounting plate — 60 × 41 × 1.9 mm with a 25 mm
    shaft pass-through and a 31 mm square pattern of 4 × Ø3.3 M3
    mounting holes around the shaft hole."""

    def name_suffix(self) -> str:
        return f"_motor_x{self.qty}"

    def bom_key(self):
        return ("MotorBracket",)

    def _build(self):
        # Plate centered on the origin. Shaft hole offset in -X from
        # center so that x_offset measured from the left edge lands
        # at the user-spec'd 20 mm. M5 pair offset in +X from center
        # so the inset measures from the right edge.
        shaft_x = -motor_plate_length / 2 + motor_shaft_x_offset
        m5_x    =  motor_plate_length / 2 - motor_m5_x_inset
        with BuildPart() as p:
            Box(motor_plate_length, motor_plate_width, motor_plate_thick)
            with Locations((shaft_x, 0, motor_plate_thick / 2)):
                Hole(radius=motor_shaft_d / 2)
                with GridLocations(motor_mount_pitch, motor_mount_pitch, 2, 2):
                    Hole(radius=motor_mount_hole_d / 2)
            with Locations((m5_x, 0, motor_plate_thick / 2)):
                with GridLocations(0, motor_m5_pitch, 1, 2):
                    Hole(radius=motor_m5_hole_d / 2)
            # The 4 Z-parallel edges are the plate's outer corners; the
            # holes are cylindrical (no straight Z-edges), so filtering
            # by Axis.Z picks exactly the corners we want to round.
            fillet(p.edges().filter_by(Axis.Z), radius=motor_corner_fillet)
        return p.part


if __name__ == "__main__":
    FlatBracket().export()
    MotorBracket().export()
