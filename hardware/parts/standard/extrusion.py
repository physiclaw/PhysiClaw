from build123d import *

from hardware.parts.base import BasePart

# ── Shared 2020-cell parameters ───────────────────────────────────────────────
leg            =  10 * MM    # outer half-extent of one 2020 cell
default_length = 200 * MM    # default extrusion length (override per-instance)
bore_diameter  =   5 * MM    # center through-hole of each cell
corner_fillet  = 1.5 * MM    # outer vertical corner edges
rib_fillet     =   2 * MM    # inner rib vertical edges near the slot/diagonal joint

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

# ── 2040-specific parameters ──────────────────────────────────────────────────
cell_offset    = leg         # 2040 cell centers at ±10 mm along X
slot_w         =  6   * MM   # central through-channel width (X)
slot_h         = 16.4 * MM   # central through-channel height (Y)

# End-counterbore screw access on the +Y (front) face, mirrored on each
# Z end. Two per end, aligned in X with the bores at ±cell_offset.
cb_end_offset  = 10  * MM    # axial offset of CB from each end face
cb_head_d      = 11  * MM    # counterbore (head pocket) diameter
cb_head_depth  =  6  * MM    # counterbore depth
cb_shaft_d     = 5.5 * MM    # through-hole diameter


# ── Helpers ───────────────────────────────────────────────────────────────────
def _cell_face():
    """One full 2020 cross-section face — 1/8 wedge mirrored across the
    hypotenuse, then across YZ and XZ, into the 4-way symmetric profile.
    Bore is NOT subtracted here so the caller can place cells freely."""
    with BuildSketch() as sk:
        with BuildLine():
            Polyline(*wedge_vertices, close=True)
        make_face()
        mirror(about=hypotenuse_plane)
        mirror(about=Plane.YZ)
        mirror(about=Plane.XZ)
    return sk.sketch


def _near(a, b):
    return abs(abs(a) - b) < 0.1


def _outer_corner_edges(z_edges, half_x):
    """Z-parallel edges at (±half_x, ±leg) — the 4 true outer corners."""
    return [
        e for e in z_edges
        if _near(e.center().X, half_x) and _near(e.center().Y, leg)
    ]


def _rib_edges(z_edges, cell_offsets):
    """For each cell centered at X = ox, the 8 rib edges where the slot
    cut meets the diagonal rib, at local (±rx, ±ry) and (±ry, ±rx) with
    (rx, ry) = wedge_vertices[2]."""
    rx, ry = wedge_vertices[2]
    edges = []
    for ox in cell_offsets:
        for e in z_edges:
            dx = e.center().X - ox
            y  = e.center().Y
            if (_near(dx, rx) and _near(y, ry)) or (_near(dx, ry) and _near(y, rx)):
                edges.append(e)
    return edges


# ── Geometry ──────────────────────────────────────────────────────────────────
class Extrusion2020(BasePart):
    def __init__(self, length: float = default_length, qty: int = 1):
        super().__init__(qty=qty)
        self.length = length

    def name_suffix(self) -> str:
        return f"_2020_{int(self.length)}mm_x{self.qty}"

    def _build(self):
        face = _cell_face()
        with BuildPart() as p:
            with BuildSketch():
                add(face)
                Circle(bore_diameter / 2, mode=Mode.SUBTRACT)
            extrude(amount=self.length)

            z_edges = p.edges().filter_by(Axis.Z)
            fillet(_outer_corner_edges(z_edges, leg), radius=corner_fillet)
            fillet(_rib_edges(z_edges, (0,)), radius=rib_fillet)

        return p.part


class Extrusion2040(BasePart):
    """2040 = two 2020 cross-sections unioned at sketch level, then
    extruded. Stacking happens before fillet so the inner contact at X=0
    disappears and only the four true outer corners get rounded."""

    def __init__(self, length: float = default_length, qty: int = 1):
        super().__init__(qty=qty)
        self.length = length

    def name_suffix(self) -> str:
        return f"_2040_{int(self.length)}mm_x{self.qty}"

    def _build(self):
        face = _cell_face()
        cell_centers = ((-cell_offset, 0), (cell_offset, 0))
        with BuildPart() as p:
            with BuildSketch():
                with Locations(*cell_centers):
                    add(face)
                with Locations(*cell_centers):
                    Circle(bore_diameter / 2, mode=Mode.SUBTRACT)
                Rectangle(slot_w, slot_h, mode=Mode.SUBTRACT)
            extrude(amount=self.length)

            z_edges = p.edges().filter_by(Axis.Z)
            fillet(_outer_corner_edges(z_edges, 2 * leg), radius=corner_fillet)
            fillet(_rib_edges(z_edges, (-cell_offset, cell_offset)), radius=rib_fillet)

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
            # Through shaft + head pocket — sketched on the same plane, two
            # separate extrudes so depths can differ.
            with BuildSketch(front_plane):
                with Locations(*cb_centers):
                    Circle(cb_shaft_d / 2)
            extrude(amount=2 * leg, mode=Mode.SUBTRACT)
            with BuildSketch(front_plane):
                with Locations(*cb_centers):
                    Circle(cb_head_d / 2)
            extrude(amount=cb_head_depth, mode=Mode.SUBTRACT)

        return p.part


if __name__ == "__main__":
    Extrusion2020().export()
    Extrusion2040().export()
