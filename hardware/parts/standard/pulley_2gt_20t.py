import math

from build123d import *

from hardware.parts.base import BasePart

# ── Parameters ────────────────────────────────────────────────────────────────
# GT2 timing pulley, 20 teeth, 6 mm belt.
flange_diameter = 18   * MM    # outer flange OD
bore_diameter   =  5   * MM    # through-bore for the motor shaft
pulley_height   =  8.5 * MM    # overall axial length

belt_width      =  6   * MM    # toothed band axial length
tooth_pitch     =  2   * MM    # 2GT
tooth_count     = 20
tooth_depth     = 0.76 * MM    # GT2 tooth-gap radial depth

# Derived: pitch diameter on which the belt sits (D = N·p / π).
pitch_diameter  = tooth_count * tooth_pitch / math.pi

# Bearing-look ring grooves: thin annular indents on top and bottom faces
# — visual race-separator lines. List of (inner_d, outer_d) pairs.
ring_grooves    = [
    (5.5 * MM, 5.7 * MM),
    (6.2 * MM, 6.4 * MM),
]
ring_depth      = 0.3 * MM


# ── Geometry ──────────────────────────────────────────────────────────────────
class Pulley2GT20T(BasePart):
    def __init__(self, toothed: bool = True, qty: int = 1):
        super().__init__(qty=qty)
        self.toothed = toothed

    def name_suffix(self) -> str:
        variant = "" if self.toothed else "_smooth"
        return f"{variant}_x{self.qty}"

    def _build(self):
        flange_thick = (pulley_height - belt_width) / 2
        with BuildPart() as p:
            # Stack from z=0: bottom flange, belt band, top flange.
            Cylinder(
                radius=flange_diameter / 2,
                height=flange_thick,
                align=(Align.CENTER, Align.CENTER, Align.MIN),
            )
            with Locations((0, 0, flange_thick)):
                Cylinder(
                    radius=pitch_diameter / 2,
                    height=belt_width,
                    align=(Align.CENTER, Align.CENTER, Align.MIN),
                )
            with Locations((0, 0, flange_thick + belt_width)):
                Cylinder(
                    radius=flange_diameter / 2,
                    height=flange_thick,
                    align=(Align.CENTER, Align.CENTER, Align.MIN),
                )

            if self.toothed:
                # 20 tooth notches around the belt band. Boxes extend 0.5 mm
                # past the band surface (overshoot) for a clean radial cut.
                overshoot = 0.5 * MM
                radial_center = pitch_diameter / 2 - tooth_depth / 2 + overshoot / 2
                chord = math.pi * pitch_diameter / tooth_count / 2   # half-pitch chord
                band_z_center = flange_thick + belt_width / 2
                with Locations((0, 0, band_z_center)):
                    with PolarLocations(radial_center, tooth_count):
                        Box(
                            chord,
                            tooth_depth + overshoot,
                            belt_width,
                            mode=Mode.SUBTRACT,
                        )

            # Through-bore (overshoot top + bottom for a clean cut).
            Cylinder(
                radius=bore_diameter / 2,
                height=pulley_height + 1 * MM,
                align=(Align.CENTER, Align.CENTER, Align.MIN),
                mode=Mode.SUBTRACT,
            )

            # Bearing-look annular grooves on top and bottom faces.
            for z in (0, pulley_height - ring_depth):
                for inner_d, outer_d in ring_grooves:
                    with BuildSketch(Plane(origin=(0, 0, z))):
                        Circle(outer_d / 2)
                        Circle(inner_d / 2, mode=Mode.SUBTRACT)
                    extrude(amount=ring_depth, mode=Mode.SUBTRACT)

        return p.part


if __name__ == "__main__":
    Pulley2GT20T(toothed=True).build()
    Pulley2GT20T(toothed=False).build()
