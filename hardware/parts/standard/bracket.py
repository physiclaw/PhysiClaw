from build123d import *

from hardware.parts.base import BasePart

# ── Parameters ────────────────────────────────────────────────────────────────
plate_width    = 40 * MM    # W, along X
plate_height   = 18 * MM    # H, along Y
plate_thick    =  4 * MM    # T, along Z

# Two inline through-holes along W, centered in H.
hole_diameter  = 5.5 * MM
hole_spacing   = 20  * MM   # center-to-center


# ── Geometry ──────────────────────────────────────────────────────────────────
class FlatBracket(BasePart):
    def _build(self):
        with BuildPart() as p:
            Box(plate_width, plate_height, plate_thick)
            with Locations((0, 0, plate_thick / 2)):
                with GridLocations(hole_spacing, 0, 2, 1):
                    Hole(radius=hole_diameter / 2)
        return p.part


if __name__ == "__main__":
    FlatBracket().export()
