from build123d import *

from hardware.parts.base import BasePart

# ── Parameters ────────────────────────────────────────────────────────────────
body_side        = 42 * MM
default_height   = 34 * MM    # default body height (override per-instance)

# Mounting: 4× M3 threaded holes on a 31 × 31 square, on the top face.
mount_spacing    = 31 * MM    # hole-center to hole-center along each axis
mount_hole_dia   =  3 * MM
mount_hole_depth =  4 * MM

# Pilot pad and output shaft (concentric, on the top face).
pad_diameter     = 22 * MM
pad_height       =  2 * MM
rod_diameter     =  5 * MM
rod_length       = 23 * MM    # measured from the top cube face

# D-cut on the shaft.
d_cut_length     = 18 * MM    # length of the flat, from rod tip downward
d_cut_flat_dist  =  2 * MM    # perpendicular distance from rod center to flat
                              # (5 mm rod with 0.5 mm flat → 4.5 mm across-flats)

# Chamfer on the 4 outer vertical corners of the body.
body_chamfer     =  5 * MM


# ── Geometry ──────────────────────────────────────────────────────────────────
class Nema17Motor(BasePart):
    def __init__(self, height: float = default_height, qty: int = 1):
        super().__init__(qty=qty)
        self.height = height

    def name_suffix(self) -> str:
        return f"_{int(self.height)}mm_x{self.qty}"

    def _build(self):
        with BuildPart() as p:
            Box(body_side, body_side, self.height)

            top_z = self.height / 2

            # Top face: 4× M3 threaded holes on a 31 mm square
            with Locations((0, 0, top_z)):
                with GridLocations(mount_spacing, mount_spacing, 2, 2):
                    Hole(radius=mount_hole_dia / 2, depth=mount_hole_depth)

            # Pilot pad (centered, sits on top face)
            with Locations((0, 0, top_z + pad_height / 2)):
                Cylinder(radius=pad_diameter / 2, height=pad_height)

            # Output shaft (concentric with pad; rod_length above top face)
            with Locations((0, 0, top_z + rod_length / 2)):
                Cylinder(radius=rod_diameter / 2, height=rod_length)

            # D-cut: flat on the +Y side, top d_cut_length of the rod.
            rod_top_z    = top_z + rod_length
            cut_z_center = rod_top_z - d_cut_length / 2
            cut_y_far    = rod_diameter      # well past the rod surface
            cut_y_size   = cut_y_far - d_cut_flat_dist
            cut_y_center = (d_cut_flat_dist + cut_y_far) / 2
            with Locations((0, cut_y_center, cut_z_center)):
                Box(rod_diameter * 2, cut_y_size, d_cut_length, mode=Mode.SUBTRACT)

            # Chamfer: 4 outer vertical edges of the body — Z-parallel edges
            # whose midpoint sits at (±body_side/2, ±body_side/2).
            half = body_side / 2
            cube_corners = [
                e for e in p.edges().filter_by(Axis.Z)
                if abs(abs(e.center().X) - half) < 0.1
                and abs(abs(e.center().Y) - half) < 0.1
            ]
            chamfer(cube_corners, length=body_chamfer)

        return p.part


if __name__ == "__main__":
    Nema17Motor().build()
