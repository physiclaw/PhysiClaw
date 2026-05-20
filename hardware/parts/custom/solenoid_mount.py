from build123d import *

from hardware.parts._fits import M3_NORMAL
from hardware.parts.base import BasePart

# ── Plate dimensions (the "keyboard") ─────────────────────────────────────────
length    = 40 * MM
width     = 29 * MM
thickness =  2 * MM

# ── Back wall (the "screen"), extruded up from the +Y edge of the plate ───────
wall_thickness = 4  * MM    # along Y
wall_height    = 35 * MM    # extruded up (+Z)
wall_center_y  = width / 2 - wall_thickness / 2

# ── Keyboard face: 4 corner through-holes ─────────────────────────────────────
keyboard_hole_diameter      = M3_NORMAL
keyboard_rect_w             = length - 8  * MM   # 32 mm
keyboard_rect_h             = width  - 12 * MM   # 17 mm
keyboard_rect_bottom_offset = 4 * MM             # rect bottom above -Y edge of face
keyboard_rect_center_y      = -width / 2 + keyboard_rect_bottom_offset + keyboard_rect_h / 2

# ── Screen face: paired hole pattern (N bases × 2 row-shifts × 2 paired rows) ─
# Hole count = 4 × len(screen_pattern_y_offsets).
screen_pattern_hole_diameter    = M3_NORMAL
screen_pattern_spacing          = 4  * MM   # face-local X between consecutive circles
screen_pattern_base_from_left   = 12 * MM   # X of first circle (= base) from left edge of face
screen_pattern_base_from_bottom = 3  * MM   # base-row Y from bottom edge of face
screen_pattern_y_offsets        = (3, 0, 2, 4, 1)   # mm above base-row, one per circle (i=0 is first)
screen_pattern_pair_offset      = 15 * MM   # paired hole sits this far above each circle
screen_pattern_row_shift        = 5  * MM   # second 10-hole row sits this far above the first

# ── Screen face: 2 corner mount holes ─────────────────────────────────────────
screen_corner_hole_diameter    = M3_NORMAL
screen_corner_hole_from_bottom = 12 * MM    # face-local Y from bottom edge
screen_corner_hole_from_side   = 4  * MM    # face-local X from each side edge

# ── Fillets ───────────────────────────────────────────────────────────────────
x_edge_fillet_radius = 0.5 * MM   # all X-parallel edges except the keyboard↔screen join


class SolenoidMount(BasePart):
    def _build(self):
        with BuildPart() as my_part:
            Box(length, width, thickness)

            # Back wall — extruded up from the +Y strip of the top face
            top_plane = Plane.XY.offset(thickness / 2)
            with BuildSketch(top_plane):
                with Locations((0, wall_center_y)):
                    Rectangle(length, wall_thickness)
            extrude(amount=wall_height)

            # Keyboard face: 4 corner through-holes
            with BuildSketch(top_plane):
                with Locations((0, keyboard_rect_center_y)):
                    with GridLocations(keyboard_rect_w, keyboard_rect_h, 2, 2):
                        Circle(radius=keyboard_hole_diameter / 2)
            extrude(amount=-thickness, mode=Mode.SUBTRACT)

            # Screen face plane (-Y normal). Origin at bottom-left corner of the face;
            # x_dir = +X forces y_dir = z_dir × x_dir = (0, 0, +1) = world +Z, so
            # face-local (x, y) reads as (from-left, above-bottom).
            screen_plane = Plane(
                origin=(-length / 2, width / 2 - wall_thickness, thickness / 2),
                x_dir=(1, 0, 0),
                z_dir=(0, -1, 0),
            )

            # Screen face: paired hole pattern (N bases × 2 row-shifts × 2 paired rows)
            screen_pattern_locations = [
                (screen_pattern_base_from_left   + i * screen_pattern_spacing,
                 screen_pattern_base_from_bottom + screen_pattern_y_offsets[i] * MM)
                for i in range(len(screen_pattern_y_offsets))
            ]
            with BuildSketch(screen_plane):
                with Locations(*screen_pattern_locations):
                    with Locations((0, 0), (0, screen_pattern_row_shift)):
                        with Locations((0, 0), (0, screen_pattern_pair_offset)):
                            Circle(radius=screen_pattern_hole_diameter / 2)
            extrude(amount=-wall_thickness, mode=Mode.SUBTRACT)

            # Screen face: 2 corner mount holes
            with BuildSketch(screen_plane):
                with Locations(
                    (         screen_corner_hole_from_side, screen_corner_hole_from_bottom),
                    (length - screen_corner_hole_from_side, screen_corner_hole_from_bottom),
                ):
                    Circle(radius=screen_corner_hole_diameter / 2)
            extrude(amount=-wall_thickness, mode=Mode.SUBTRACT)

            # Fillet: all X-parallel edges except the keyboard↔screen inner join
            # (the one at world (y = width/2 - wall_thickness, z = thickness/2)).
            join_y = width / 2 - wall_thickness
            join_z = thickness / 2
            x_fillet_edges = [
                e for e in my_part.edges().filter_by(Axis.X)
                if not (abs(e.center().Y - join_y) < 1e-3
                        and abs(e.center().Z - join_z) < 1e-3)
            ]
            fillet(x_fillet_edges, radius=x_edge_fillet_radius)

        return my_part.part


if __name__ == "__main__":
    SolenoidMount().export()
