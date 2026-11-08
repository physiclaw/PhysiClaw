"""Reusable GT2 belt geometry; callers supply fully positioned waypoints."""

import math
from itertools import pairwise

from build123d import MM, Solid, Vector, Wire

from hardware.parts.base import BaseStandardPart
from hardware.parts.standard.pulley import belt_width as pulley_belt_width
from hardware.parts.standard.pulley import pitch_diameter as pulley_pitch_diameter

# ── GT2 belt parameters ───────────────────────────────────────────────────────
belt_width = pulley_belt_width  # along pulley / idler axes
belt_thickness = 1.38 * MM  # GT2 total thickness (backing + tooth)
idler_pitch_r = pulley_pitch_diameter / 2  # ≈ 6.37 mm
wrap_r = idler_pitch_r + belt_thickness / 2  # belt centerline radius at wrap


def _expand_wraps(waypoints, step_deg=10):
    """Convert a list of (position, radius) waypoints into a denser polyline
    that has straight tangent segments between waypoints and short
    polyline approximations of the wrap arcs at each interior waypoint.

    Each interior waypoint is wrapped with its own radius. A radius of None
    marks an explicit path point with no wrap; endpoints are always explicit.
    Wrap direction (CCW vs CW in XZ) follows the path's turn at that waypoint —
    positive cross product of (entry-direction × exit-direction) ⇒ CCW.

    Y is preserved per waypoint; the connecting segments carry any
    Y-twist between waypoints.
    """

    def _norm_xz(vx, vz):
        n = math.sqrt(vx * vx + vz * vz)
        return (vx / n, vz / n) if n > 1e-9 else (0.0, 0.0)

    n = len(waypoints)
    if n < 2:
        return [p for p, _ in waypoints]

    out = [waypoints[0][0]]
    for i in range(1, n - 1):
        A = waypoints[i - 1][0]
        C, r = waypoints[i]
        B = waypoints[i + 1][0]
        if r is None:
            out.append(C)
            continue
        D_in_x, D_in_z = _norm_xz(C[0] - A[0], C[2] - A[2])
        D_out_x, D_out_z = _norm_xz(B[0] - C[0], B[2] - C[2])
        cross = D_in_x * D_out_z - D_in_z * D_out_x
        ccw = cross >= 0  # default CCW for the 180° (cross ≈ 0) case
        if ccw:
            in_rx, in_rz = (D_in_z, -D_in_x)
            out_rx, out_rz = (D_out_z, -D_out_x)
        else:
            in_rx, in_rz = (-D_in_z, D_in_x)
            out_rx, out_rz = (-D_out_z, D_out_x)

        theta_in = math.atan2(in_rz, in_rx)
        theta_out = math.atan2(out_rz, out_rx)
        sweep = theta_out - theta_in if ccw else theta_in - theta_out
        if sweep <= 1e-6:
            sweep += 2 * math.pi

        n_steps = max(2, int(math.degrees(sweep) / step_deg))
        for k in range(n_steps + 1):
            t = k / n_steps
            theta = theta_in + (sweep if ccw else -sweep) * t
            ax = C[0] + r * math.cos(theta)
            az = C[2] + r * math.sin(theta)
            ay = C[1]
            out.append((ax, ay, az))

    out.append(waypoints[-1][0])
    return out


def _perp_y(z_dir):
    """Direction perpendicular to z_dir with maximum +Y projection — used as
    the cross-section x-axis so the belt's width axis tracks world Y. For
    segments lying in world XZ this returns (0, 1, 0) exactly; segments
    that shift in Y get a small tilt so the section stays perpendicular
    to the centerline. Falls back to +X for the degenerate case where
    z_dir is parallel to world Y."""
    zx, zy, zz = z_dir
    px, py, pz = -zy * zx, 1 - zy * zy, -zy * zz
    length = math.sqrt(px * px + py * py + pz * pz)
    if length < 1e-6:
        return (1, 0, 0)
    return (px / length, py / length, pz / length)


class Belt(BaseStandardPart):
    """GT2 belt with shared miter sections along a polyline centerline.

    Adjacent segments use the same section at each bend, so their surfaces
    meet without the notches and exposed end caps of independent extrusions.
    Width tracks world Y (the pulley / idler axis direction).

    Route selection and motor placement belong to hardware.assembly.belt_routing.
    """

    def __init__(self, path, name="", qty=1):
        super().__init__(qty=qty)
        self.path = tuple((tuple(p), r) for p, r in path)
        self.name = name

    def name_suffix(self) -> str:
        return f"_{self.name}_x{self.qty}" if self.name else f"_x{self.qty}"

    def bom_key(self):
        # Belt is a continuous strip cut to length; one BOM line per machine.
        return ("Belt", "GT2", "loop")

    def geom_key(self):
        # Purchasable identity is shared; each routed shape is distinct.
        return ("Belt", self.path)

    def _build(self):
        points = []
        for point in map(Vector, _expand_wraps(self.path)):
            if not points or (point - points[-1]).length > 1e-6:
                points.append(point)
        if len(points) < 2:
            raise ValueError("Belt path must contain a nonzero-length segment")
        directions = [(b - a).normalized() for a, b in pairwise(points)]
        sections = []
        for i, point in enumerate(points):
            incoming = directions[max(0, i - 1)]
            outgoing = directions[min(i, len(directions) - 1)]
            bisector = incoming + outgoing
            if bisector.length < 1e-6:
                raise ValueError("Belt path reversals need a rounded bend")
            normal = bisector.normalized()
            width = Vector(_perp_y(tuple(incoming))) * (belt_width / 2)
            thickness = incoming.cross(width.normalized()) * (belt_thickness / 2)
            corners = []
            for w, t in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
                offset = w * width + t * thickness
                # Intersect the incoming segment's four longitudinal edges
                # with the common bisector plane to form a true miter.
                offset -= incoming * (normal.dot(offset) / normal.dot(incoming))
                corners.append(point + offset)
            sections.append(Wire.make_polygon(corners, close=True))
        segments = [Solid.make_loft([a, b], ruled=True) for a, b in pairwise(sections)]
        # One n-ary union also merges the intermeshed belt envelopes in the
        # installed clamp, where the route intentionally overlaps itself.
        return segments[0].fuse(*segments[1:]) if len(segments) > 1 else segments[0]


if __name__ == "__main__":
    # Preserve the standalone exporter as an explicit composition entry point.
    from hardware.assembly.belt_routing import motor_path

    for motor in ("A", "B"):
        Belt(path=motor_path(motor), name=f"motor_{motor.lower()}").export()
