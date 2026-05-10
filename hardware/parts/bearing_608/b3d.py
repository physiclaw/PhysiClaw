"""608ZZ deep-groove ball bearing — build123d twin.

Simplified as an annular cylinder. Real 608ZZ has races, balls, and
shields; for assembly modeling and clearance checks the outer envelope
is what matters. Mirrors `parts/bearing_608/fc.py`.
"""

from build123d import (  # type: ignore[import-not-found]
    BuildPart,
    BuildSketch,
    Circle,
    Mode,
    Part,
    extrude,
)

from parts import StandardPart
from parts.bearing_608.spec import BEARING_608


class Bearing608(StandardPart):
    output_name = "Bearing_608"

    def build(self) -> Part:
        spec = self.spec
        with BuildPart() as bp:
            with BuildSketch():
                Circle(spec.outer_diameter_mm / 2)
                Circle(spec.inner_diameter_mm / 2, mode=Mode.SUBTRACT)
            extrude(amount=spec.width_mm)
        return bp.part


PART = Bearing608(BEARING_608)
