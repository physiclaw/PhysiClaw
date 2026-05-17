from build123d import *

from hardware.parts._fits import M4_CLOSE, M4_NUT_T, M4_NUT_W, M5_CLOSE, M5_NORMAL, M5_NUT_T, M5_NUT_W
from hardware.parts.base import BasePart

# ── Block dimensions ──────────────────────────────────────────────────────────
length    = 40 * MM
width     = 20 * MM
thickness = 18 * MM

# ── Top face: 3 holes in a row (middle one counterbored) ──────────────────────
top_center_hole_diameter = M5_NORMAL
top_counterbore_diameter = 10  * MM
top_counterbore_depth    = 13  * MM
top_outer_hole_diameter  = M4_CLOSE
top_outer_hole_offset    = 11  * MM   # from center hole, along X

# ── Top face: M5 square-nut socket near the -Y edge ───────────────────────────
top_pocket_w             = M5_NUT_W
top_pocket_h             = M5_NUT_T
top_pocket_bottom_offset = 3.5 * MM   # bottom of rect above -Y edge of face
top_pocket_depth         = 12  * MM
top_pocket_center_y      = -width / 2 + top_pocket_bottom_offset + top_pocket_h / 2

# ── Front face: M5 clearance hole, centered in X ──────────────────────────────
front_hole_diameter   = M5_CLOSE
front_hole_top_offset = 8 * MM   # hole center below top edge
front_hole_depth      = 6 * MM
front_hole_center_z   = (thickness / 2) - front_hole_top_offset

# ── Left + right faces: M4 square-nut sockets (one per side) ──────────────────
side_pocket_w          = M4_NUT_W     # along Y
side_pocket_h          = M4_NUT_T     # along Z
side_pocket_top_offset = 4   * MM     # upper edge below top edge of face
side_pocket_depth      = 13  * MM     # along X (into the block)
side_pocket_center_z   = (thickness / 2) - side_pocket_top_offset - side_pocket_h / 2
side_pocket_center_x   = (length - side_pocket_depth) / 2

# ── Fillets ───────────────────────────────────────────────────────────────────
cube_corner_fillet_radius = 2 * MM


class PulleyMountMotor(BasePart):
    def _build(self):
        with BuildPart() as my_part:
            Box(length, width, thickness)

            # Top: counterbore at center + two outer through-holes
            with Locations((0, 0, thickness / 2)):
                CounterBoreHole(
                    radius=top_center_hole_diameter / 2,
                    counter_bore_radius=top_counterbore_diameter / 2,
                    counter_bore_depth=top_counterbore_depth,
                )
            with Locations((-top_outer_hole_offset, 0), (top_outer_hole_offset, 0)):
                Hole(radius=top_outer_hole_diameter / 2)

            # Top: rectangle pocket
            top_plane = Plane.XY.offset(thickness / 2)
            with BuildSketch(top_plane):
                with Locations((0, top_pocket_center_y)):
                    Rectangle(top_pocket_w, top_pocket_h)
            extrude(amount=-top_pocket_depth, mode=Mode.SUBTRACT)

            # Front: M4 hole
            # x_dir = +X forces y_dir = z_dir × x_dir = (0, 0, +1) = world +Z.
            front_plane = Plane(origin=(0, -width / 2, 0), x_dir=(1, 0, 0), z_dir=(0, -1, 0))
            with BuildSketch(front_plane):
                with Locations((0, front_hole_center_z)):
                    Circle(radius=front_hole_diameter / 2)
            extrude(amount=-front_hole_depth, mode=Mode.SUBTRACT)

            # Sides: left + right rectangle pockets — both cavities in one OCCT op.
            with Locations(
                (-side_pocket_center_x, 0, side_pocket_center_z),
                ( side_pocket_center_x, 0, side_pocket_center_z),
            ):
                Box(side_pocket_depth, side_pocket_w, side_pocket_h, mode=Mode.SUBTRACT)

            # Fillet: four outer cube vertical corners — longest Z-parallel edges (= thickness).
            cube_corners = my_part.edges().filter_by(Axis.Z).group_by(Edge.length)[-1]
            fillet(cube_corners, radius=cube_corner_fillet_radius)

        return my_part.part


if __name__ == "__main__":
    PulleyMountMotor().build()
