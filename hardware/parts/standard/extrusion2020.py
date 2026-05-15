from build123d import *

from hardware.parts.base import BasePart

# ── Parameters ────────────────────────────────────────────────────────────────
leg            =  10 * MM    # outer half-extent of the 20×20 — used to find the 4 corner edges
default_length = 200 * MM    # default extrusion length (override per-instance)
bore_diameter  =   5 * MM    # center through-hole
corner_fillet  = 1.5 * MM    # 4 outer vertical corner edges
rib_fillet     =   2 * MM    # 8 inner rib vertical edges (near the center)

# 1/8 cross-section outline (one slot in profile). Traces, in order:
# center → bottom edge → slot notch → right edge → top corner → hypotenuse back.
wedge_vertices = (
    (0,         0),
    (3.9  * MM, 0),
    (3.9  * MM, 2.84 * MM),
    (6.56 * MM, 5.5  * MM),
    (8.2  * MM, 5.5  * MM),
    (8.2  * MM, 3.1  * MM),
    (9.5  * MM, 3.1  * MM),
    (9.5  * MM, 3.6  * MM),
    (10   * MM, 3.6  * MM),
    (10   * MM, 10   * MM),
)

# Mirror plane through the hypotenuse (line y = x in the XY plane), extending
# up Z. x_dir along the hypotenuse; z_dir is the plane normal in XY.
hypotenuse_plane = Plane(origin=(0, 0, 0), x_dir=(1, 1, 0), z_dir=(1, -1, 0))


# ── Geometry ──────────────────────────────────────────────────────────────────
class Extrusion2020(BasePart):
    def __init__(self, length: float = default_length, qty: int = 1):
        super().__init__(qty=qty)
        self.length = length

    def name_suffix(self) -> str:
        return f"_{int(self.length)}mm_x{self.qty}"

    def _build(self):
        with BuildPart() as p:
            with BuildSketch():
                # 1/8 wedge outline including the slot-notch cut directly.
                with BuildLine():
                    Polyline(*wedge_vertices, close=True)
                make_face()
                # Mirror across the hypotenuse → 1/4 wedge with the slot
                # reflected to the symmetric position.
                mirror(about=hypotenuse_plane)
                # Mirror across YZ (x=0) and XZ (y=0) → full 4-way symmetric
                # cross-section, one T-slot on each face.
                mirror(about=Plane.YZ)
                mirror(about=Plane.XZ)
                # Center through-hole
                Circle(bore_diameter / 2, mode=Mode.SUBTRACT)
            extrude(amount=self.length)

            z_edges = p.edges().filter_by(Axis.Z)

            # Fillet: 4 outer vertical edges — Z-parallel edges whose midpoint
            # sits at (±leg, ±leg) (the slot cuts and bore are all inboard).
            outer_corners = [
                e for e in z_edges
                if abs(abs(e.center().X) - leg) < 0.1
                and abs(abs(e.center().Y) - leg) < 0.1
            ]
            fillet(outer_corners, radius=corner_fillet)

            # Fillet: 8 inner rib edges where the slot cut meets the diagonal
            # rib. These sit at wedge_vertices[2] = (3.9, 2.84) and its
            # symmetric mirrors → (±3.9, ±2.84) and (±2.84, ±3.9).
            rx, ry = wedge_vertices[2]
            rib_edges = [
                e for e in z_edges
                if (abs(abs(e.center().X) - rx) < 0.1 and abs(abs(e.center().Y) - ry) < 0.1)
                or (abs(abs(e.center().X) - ry) < 0.1 and abs(abs(e.center().Y) - rx) < 0.1)
            ]
            fillet(rib_edges, radius=rib_fillet)

        return p.part


if __name__ == "__main__":
    Extrusion2020().build()
