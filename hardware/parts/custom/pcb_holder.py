from build123d import *

from hardware.parts._fits import CSK_ANGLE, M3_CSK_HEAD, M3_NORMAL, M3_NUT_W
from hardware.parts.base import BaseCustomPart

# ── Plate ─────────────────────────────────────────────────────────────────────
thickness = 3 * MM

# ── PCB mounting pattern ──────────────────────────────────────────────────────
# PCB is 90 × 70 with four corner M3 holes spaced 82 × 62 (i.e. 4 mm in from
# each PCB edge). Holder mirrors that pattern about its center.
hole_pitch_x       = 82 * MM
hole_pitch_y       = 62 * MM
hole_x             = hole_pitch_x / 2          # 41
hole_y             = hole_pitch_y / 2          # 31
standoff_xy        = [(sx, sy) for sx in (-hole_x, hole_x)
                                for sy in (-hole_y, hole_y)]

# Standoff: the PCB rests on top of these; the screw passes through the bore
# and the plate into a square nut held against the underside.
cyl_d              = 8 * MM
cyl_h              = 4 * MM

# Square-nut locating mark: a shallow square recess on the plate's underside
# (opposite the cylinder side), centered under each bore — shows where to seat
# the M3 square nut. A locating mark, not a deep capture pocket.
nut_mark_size      = M3_NUT_W   # square nut width across flats
nut_mark_depth     = 1.0 * MM

# Plate sized to enclose the standoffs with a small margin.
plate_margin       = 3 * MM
plate_half_x       = hole_x + cyl_d / 2 + plate_margin   # 48 → 96 mm wide
plate_half_y       = hole_y + cyl_d / 2 + plate_margin   # 38 → 76 mm deep

# Mounting rib on the underside (the square-nut side): runs along X, set in
# rib_gap from the +X short edge, centered in Y. Protrudes 3 mm.
tab_x     = 38 * MM   # along X — the rib's long side
tab_y     = 10 * MM   # along Y — the rib's short side
tab_thick = 3  * MM   # protrudes below the plate (-Z)
rib_gap   = 9  * MM   # gap between the rib's +X end and the plate short edge
rib_cx    = plate_half_x - rib_gap - tab_x / 2   # rib center in X
rib_cy    = 0  * MM                              # rib center in Y

# 2040 mount: two M3 countersink holes through plate + rib, recessed on the
# cylinder side (top), threading into the extrusion T-slot below the rib.
mount_hole_pitch = 20 * MM   # along X, centered on the rib

# ── Lightening ────────────────────────────────────────────────────────────────
# Capsule cut-outs through the plate only (never the rib) — the PCB is light, so
# cut aggressively. A staggered grid, filtered against the standoffs, the rib
# footprint and a plate-edge border.
cap_len     = 18  * MM
cap_wid     = 8   * MM
cap_wall_x  = 4   * MM           # end-to-end gap along the capsule
cap_wall_y  = 3.5 * MM           # gap between rows
cap_margin  = 4   * MM           # plate-edge border
cap_keepout = cyl_d / 2 + 2.5 * MM   # clearance ring around each standoff
rib_keepout = 1.5 * MM           # clearance around the rib footprint (thin wall only)

# Round the four plate corners (d = 4 mm → radius 2 mm).
corner_d = 4 * MM
edge_tol = 0.5 * MM   # fuzz for matching fillet edges by center


def standoff_locs(z):
    """The four standoff (x, y) positions lifted to height z."""
    return [(sx, sy, z) for sx, sy in standoff_xy]


def _lightening_centers():
    pitch_x, pitch_y = cap_len + cap_wall_x, cap_wid + cap_wall_y
    hx, hy = cap_len / 2, cap_wid / 2
    keepouts = [(sx - cap_keepout, sx + cap_keepout,
                 sy - cap_keepout, sy + cap_keepout) for sx, sy in standoff_xy]
    keepouts.append((rib_cx - tab_x / 2 - rib_keepout, rib_cx + tab_x / 2 + rib_keepout,
                     rib_cy - tab_y / 2 - rib_keepout, rib_cy + tab_y / 2 + rib_keepout))
    nx = int(plate_half_x / pitch_x) + 2
    ny = int(plate_half_y / pitch_y) + 2
    centers = []
    for j in range(-ny, ny + 1):
        cy = j * pitch_y
        x_off = pitch_x / 2 if j % 2 else 0          # brick stagger
        for i in range(-nx, nx + 1):
            cx = i * pitch_x + x_off
            if (cx - hx < -plate_half_x + cap_margin or cx + hx > plate_half_x - cap_margin
                    or cy - hy < -plate_half_y + cap_margin or cy + hy > plate_half_y - cap_margin):
                continue
            if any(cx - hx < x1 and cx + hx > x0 and cy - hy < y1 and cy + hy > y0
                   for x0, x1, y0, y1 in keepouts):
                continue
            centers.append((cx, cy))
    return centers


class PcbHolder(BaseCustomPart):
    def _build(self):
        with BuildPart() as my_part:
            # Base plate: z = 0 .. thickness.
            Box(2 * plate_half_x, 2 * plate_half_y, thickness,
                align=(Align.CENTER, Align.CENTER, Align.MIN))

            # Mounting rib on the underside, centered at (rib_cx, rib_cy),
            # protruding -Z by tab_thick.
            with Locations((rib_cx, rib_cy, 0)):
                Box(tab_x, tab_y, tab_thick,
                    align=(Align.CENTER, Align.CENTER, Align.MAX))

            # Standoff cylinders on top: z = thickness .. thickness + cyl_h.
            with Locations(*standoff_locs(thickness)):
                Cylinder(cyl_d / 2, cyl_h, align=(Align.CENTER, Align.CENTER, Align.MIN))

            # M3 clearance bore through each standoff + plate.
            with Locations(*standoff_locs(thickness + cyl_h)):
                Hole(radius=M3_NORMAL / 2)

            # Square-nut locating recess on the underside, centered under each bore.
            with Locations(*standoff_locs(0)):
                Box(nut_mark_size, nut_mark_size, nut_mark_depth,
                    align=(Align.CENTER, Align.CENTER, Align.MIN),
                    mode=Mode.SUBTRACT)

            # 2040 mount countersinks — recessed on the cylinder-side (top) face,
            # bored down through plate + rib. Centered along the rib, 20 mm apart.
            with Locations(
                (rib_cx - mount_hole_pitch / 2, rib_cy, thickness),
                (rib_cx + mount_hole_pitch / 2, rib_cy, thickness),
            ):
                CounterSinkHole(
                    radius=M3_NORMAL / 2,
                    counter_sink_radius=M3_CSK_HEAD / 2,
                    counter_sink_angle=CSK_ANGLE,
                )

            # Capsule lightening through the plate (one sketch, one subtract).
            with BuildSketch(Plane.XY):
                with Locations(*_lightening_centers()):
                    SlotOverall(cap_len, cap_wid)
            extrude(amount=thickness, mode=Mode.SUBTRACT)

            # Round the four plate corners.
            plate_corners = [
                e for e in my_part.edges().filter_by(Axis.Z)
                if abs(abs(e.center().X) - plate_half_x) < edge_tol
                and abs(abs(e.center().Y) - plate_half_y) < edge_tol
            ]
            fillet(plate_corners, radius=corner_d / 2)

        return my_part.part


if __name__ == "__main__":
    PcbHolder().export()
