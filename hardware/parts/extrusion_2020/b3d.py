"""2020 aluminum T-slot extrusion — build123d twin.

Faithful 2020 cross-section: 20×20 outer with R1.5 corners, four
T-slots (6.2 mm mouth, 1.8 mm wall, 11 mm cavity), a centre block
linked to the outer wall by four diagonal ribs, and a Ø5 axial
bore. Padded to length.
"""

import math

from build123d import (  # type: ignore[import-not-found]
    Axis,
    BuildLine,
    BuildPart,
    BuildSketch,
    Circle,
    Location,
    Locations,
    Mode,
    Part,
    Polyline,
    Rectangle,
    extrude,
    fillet,
    make_face,
)

from parts import StandardPart
from parts.extrusion_2020.spec import (
    BORDER_TO_CENTER_MM,
    EXTRUSION_2020_L300,
    FILLET_RADIUS_MM,
    OUTER_SIZE_MM,
    RIB_THICKNESS_MM,
    SLOT_CAVITY_MM,
    SLOT_MOUTH_DEPTH_MM,
    SLOT_MOUTH_MM,
)


def _rotate_pt(x: float, y: float, angle_deg: float) -> tuple[float, float]:
    a = math.radians(angle_deg)
    return (x * math.cos(a) - y * math.sin(a), x * math.sin(a) + y * math.cos(a))


class Extrusion2020(StandardPart):
    output_name = "Extrusion_2020_L300"

    def build(self) -> Part:
        spec = self.spec
        half = OUTER_SIZE_MM / 2
        centre_half = half - BORDER_TO_CENTER_MM

        # T-slot polygon — mouth opens to +Y, centred on x=0.
        t_slot_pts = [
            (-SLOT_MOUTH_MM / 2,  half),
            ( SLOT_MOUTH_MM / 2,  half),
            ( SLOT_MOUTH_MM / 2,  half - SLOT_MOUTH_DEPTH_MM),
            ( SLOT_CAVITY_MM / 2, half - SLOT_MOUTH_DEPTH_MM),
            ( SLOT_CAVITY_MM / 2, centre_half),
            (-SLOT_CAVITY_MM / 2, centre_half),
            (-SLOT_CAVITY_MM / 2, half - SLOT_MOUTH_DEPTH_MM),
            (-SLOT_MOUTH_MM / 2,  half - SLOT_MOUTH_DEPTH_MM),
        ]
        rib_len = (half - centre_half) * math.sqrt(2)
        rib_centre_dist = (centre_half + half) / 2

        with BuildPart() as bp:
            with BuildSketch():
                Rectangle(OUTER_SIZE_MM, OUTER_SIZE_MM)
                for angle in (0, 90, 180, 270):
                    rotated = [_rotate_pt(x, y, angle) for x, y in t_slot_pts]
                    with BuildLine():
                        Polyline(*rotated, close=True)
                    make_face(mode=Mode.SUBTRACT)
                Rectangle(2 * centre_half, 2 * centre_half, mode=Mode.ADD)
                for angle in (45, 135, 225, 315):
                    a = math.radians(angle)
                    cx = rib_centre_dist * math.cos(a)
                    cy = rib_centre_dist * math.sin(a)
                    with Locations(Location((cx, cy, 0), (0, 0, 1), angle)):
                        Rectangle(rib_len, RIB_THICKNESS_MM, mode=Mode.ADD)
            extrude(amount=spec.length_mm)

            # Outer corners: vertical edges where both X and Y are at ±half.
            outer_z_edges = [
                e for e in bp.edges().filter_by(Axis.Z)
                if abs(abs(e.vertices()[0].X) - half) < 1e-3
                and abs(abs(e.vertices()[0].Y) - half) < 1e-3
            ]
            fillet(outer_z_edges, radius=FILLET_RADIUS_MM)

            with BuildSketch():
                Circle(spec.bore_diameter_mm / 2)
            extrude(amount=spec.length_mm, mode=Mode.SUBTRACT)

        return bp.part


PART = Extrusion2020(EXTRUSION_2020_L300)
