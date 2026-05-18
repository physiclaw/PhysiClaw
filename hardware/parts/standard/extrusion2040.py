from build123d import *

from hardware.parts.base import BasePart
from hardware.parts.standard.extrusion2020 import (
    bore_diameter,
    hypotenuse_plane,
    leg,
    wedge_vertices,
)

# ── Parameters ────────────────────────────────────────────────────────────────
default_length = 200 * MM
corner_fillet  = 1.5 * MM    # 4 outer vertical corner edges of the 2040
rib_fillet     =   2 * MM    # 16 inner rib vertical edges (8 per cell)
slot_w         = 6 * MM      # central through-channel width (X)
slot_h         = 16.4 * MM   # central through-channel height (Y)
cell_offset    = leg         # cell centers at ±10 mm along X

# End-counterbore screw access on the +Y (front) face, mirrored on each
# Z end. Two per end, aligned in X with the bores at ±cell_offset.
cb_end_offset  = 10 * MM     # axial offset of CB from each end face
cb_head_d      = 11 * MM     # counterbore (head pocket) diameter
cb_head_depth  =  6 * MM     # counterbore depth
cb_shaft_d     = 5.5 * MM    # through-hole diameter


def _2020_face():
    """One full 2020 cross-section face, before bore and before fillet."""
    with BuildSketch() as sk:
        with BuildLine():
            Polyline(*wedge_vertices, close=True)
        make_face()
        mirror(about=hypotenuse_plane)
        mirror(about=Plane.YZ)
        mirror(about=Plane.XZ)
    return sk.sketch


# ── Geometry ──────────────────────────────────────────────────────────────────
class Extrusion2040(BasePart):
    """2040 = two 2020 cross-sections unioned at sketch level, then
    extruded. Stacking happens before fillet so the inner contact at X=0
    disappears and only the four true outer corners get rounded."""

    def __init__(self, length: float = default_length, qty: int = 1):
        super().__init__(qty=qty)
        self.length = length

    def name_suffix(self) -> str:
        return f"_{int(self.length)}mm_x{self.qty}"

    def _build(self):
        face = _2020_face()
        with BuildPart() as p:
            with BuildSketch():
                with Locations((-cell_offset, 0), (cell_offset, 0)):
                    add(face)
                # Bores at each cell center.
                with Locations((-cell_offset, 0), (cell_offset, 0)):
                    Circle(bore_diameter / 2, mode=Mode.SUBTRACT)
                # Central through-channel.
                Rectangle(slot_w, slot_h, mode=Mode.SUBTRACT)
            extrude(amount=self.length)

            z_edges = p.edges().filter_by(Axis.Z)

            # Outer corners: 4 at (±2*leg, ±leg).
            outer_corners = [
                e for e in z_edges
                if abs(abs(e.center().X) - 2 * leg) < 0.1
                and abs(abs(e.center().Y) - leg) < 0.1
            ]
            fillet(outer_corners, radius=corner_fillet)

            # Rib edges: each cell contributes 8 ribs at local
            # (±rx, ±ry) and (±ry, ±rx); world coords are offset by ±cell_offset in X.
            rx, ry = wedge_vertices[2]
            def near(a, b):
                return abs(abs(a) - b) < 0.1
            rib_edges = []
            for ox in (-cell_offset, cell_offset):
                for e in z_edges:
                    dx = e.center().X - ox
                    y  = e.center().Y
                    if (near(dx, rx) and near(y, ry)) or (near(dx, ry) and near(y, rx)):
                        rib_edges.append(e)
            fillet(rib_edges, radius=rib_fillet)

            # ── End counterbores on the +Y (front) face ──
            # Workplane on +Y face; +Z axis of plane = world +Z, normal
            # points into the part (-Y), so positive extrude drills inward.
            front_plane = Plane(
                origin=(0, leg, 0),
                x_dir=(1, 0, 0),
                z_dir=(0, -1, 0),
            )
            cb_centers = [
                (-cell_offset, cb_end_offset),
                ( cell_offset, cb_end_offset),
                (-cell_offset, self.length - cb_end_offset),
                ( cell_offset, self.length - cb_end_offset),
            ]
            # Through shafts (cuts all the way to the -Y face).
            with BuildSketch(front_plane):
                with Locations(*cb_centers):
                    Circle(cb_shaft_d / 2)
            extrude(amount=2 * leg, mode=Mode.SUBTRACT)
            # Head pockets.
            with BuildSketch(front_plane):
                with Locations(*cb_centers):
                    Circle(cb_head_d / 2)
            extrude(amount=cb_head_depth, mode=Mode.SUBTRACT)

        return p.part


if __name__ == "__main__":
    Extrusion2040().build()
