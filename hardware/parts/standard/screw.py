import math

from build123d import *

from hardware.parts.base import BasePart

# ── Dimension tables (mm) ─────────────────────────────────────────────────────
# d = nominal Ø, P = coarse pitch.
COMMON = {
    "M2":   {"d": 2.0, "P": 0.40},
    "M2.5": {"d": 2.5, "P": 0.45},
    "M3":   {"d": 3.0, "P": 0.50},
    "M4":   {"d": 4.0, "P": 0.70},
    "M5":   {"d": 5.0, "P": 0.80},
    "M6":   {"d": 6.0, "P": 1.00},
    "M8":   {"d": 8.0, "P": 1.25},
}

# dk = max head Ø, k = head height, s = hex socket across-flats (Allen key).
# SHCS — ISO 4762 / DIN 912 (head height ≈ d).
SHCS_DIMS = {
    "M2":   {"dk":  3.8, "k": 2.0, "s": 1.5},
    "M2.5": {"dk":  4.5, "k": 2.5, "s": 2.0},
    "M3":   {"dk":  5.5, "k": 3.0, "s": 2.5},
    "M4":   {"dk":  7.0, "k": 4.0, "s": 3.0},
    "M5":   {"dk":  8.5, "k": 5.0, "s": 4.0},
    "M6":   {"dk": 10.0, "k": 6.0, "s": 5.0},
    "M8":   {"dk": 13.0, "k": 8.0, "s": 6.0},
}

# FHCS — ISO 10642 / DIN 7991 (90° countersunk).
FHCS_DIMS = {
    "M3": {"dk":  6.0, "k": 1.86, "s": 2.0},
    "M4": {"dk":  8.0, "k": 2.48, "s": 2.5},
    "M5": {"dk": 10.0, "k": 3.10, "s": 3.0},
    "M6": {"dk": 12.0, "k": 3.72, "s": 4.0},
    "M8": {"dk": 16.0, "k": 4.96, "s": 5.0},
}

# BHCS — ISO 7380-1 / DIN 7380. Button heads have head height ≈ 0.5 d and the
# shallowest socket of the three (lowest torque ceiling). Not a drop-in for
# SHCS in clamp-critical joints (CoreXY corners, NEMA17 mounts).
BHCS_DIMS = {
    "M3": {"dk":  5.7, "k": 1.65, "s": 2.0},
    "M4": {"dk":  7.6, "k": 2.20, "s": 2.5},
    "M5": {"dk":  9.5, "k": 2.75, "s": 3.0},
    "M6": {"dk": 10.5, "k": 3.30, "s": 4.0},
    "M8": {"dk": 14.0, "k": 4.40, "s": 5.0},
}

# Shoulder screw (a.k.a. stripper bolt) — socket head, smooth load-bearing
# shoulder, then a shorter threaded section. Keyed by THREAD size; the
# shoulder diameter is larger than the thread. `length` on the Screw class
# is the shoulder length (the smooth rod).
SHOULDER_DIMS = {
    "M4": {
        "dk": 9.0,          # head diameter
        "k": 3.5,           # head height
        "s": 3.0,           # hex socket across-flats
        "shoulder_d": 5.0,  # smooth shoulder diameter
        "thread_len": 7.2,  # threaded section axial length
    },
}

TABLES = {
    "SHCS": SHCS_DIMS,
    "FHCS": FHCS_DIMS,
    "BHCS": BHCS_DIMS,
    "SHOULDER": SHOULDER_DIMS,
}

# Hex socket depth as a fraction of head height k.
SOCKET_DEPTH_FRAC = {"SHCS": 0.6, "FHCS": 0.6, "BHCS": 0.5, "SHOULDER": 0.6}


# ── Half-profile builders (called inside BuildLine on Plane.XZ; r ≥ 0) ────────
def _shcs_profile(d, length, dim):
    # Cylindrical shank + cylindrical cap head.
    dk, k = dim["dk"], dim["k"]
    Polyline(
        (0,      -length),
        (d / 2,  -length),
        (d / 2,   0),
        (dk / 2,  0),
        (dk / 2,  k),
        (0,       k),
        close=True,
    )


def _fhcs_profile(d, length, dim):
    # FHCS length is OVERALL (head included); shank = length − k.
    dk, k = dim["dk"], dim["k"]
    shank = length - k
    Polyline(
        (0,      -shank),
        (d / 2,  -shank),
        (d / 2,   0),
        (dk / 2,  k),
        (0,       k),
        close=True,
    )


def _bhcs_profile(d, length, dim):
    # Dome = spherical cap. Sphere center on the Z axis at z = k − R; the
    # sphere passes through the head edge (dk/2, 0) and apex (0, k):
    #   (dk/2)² + (R − k)² = R²   ⇒   R = ((dk/2)² + k²) / (2k).
    # RadiusArc sign: positive R bulges to the LEFT of the start→end vector
    # (which here points up-and-inward) — i.e. the arc would dip toward
    # the axis, giving a concave dish. Use −R so the arc bulges outward
    # (up-and-away from the axis) into a convex dome.
    dk, k = dim["dk"], dim["k"]
    R = ((dk / 2) ** 2 + k ** 2) / (2 * k)
    Polyline(
        (0,      -length),
        (d / 2,  -length),
        (d / 2,   0),
        (dk / 2,  0),
    )
    try:
        RadiusArc((dk / 2, 0), (0, k), -R)
    except Exception:
        # Fallback: 3-point spline through the mid-arc point on the dome
        # (computed from the sphere center on the Z axis).
        cz = k - R
        am = (math.atan2(-cz, dk / 2) + math.pi / 2) / 2
        mx, mz = R * math.cos(am), cz + R * math.sin(am)
        Spline((dk / 2, 0), (mx, mz), (0, k))
    Line((0, k), (0, -length))


def _shoulder_profile(d, length, dim):
    # Thread (Ø d, len thread_len) → smooth shoulder (Ø shoulder_d, len `length`)
    # → cylindrical head (Ø dk × k). z=0 sits at the underhead seating plane
    # (top of shoulder); thread occupies z ∈ [−length−thread_len, −length].
    dk, k = dim["dk"], dim["k"]
    sd, tl = dim["shoulder_d"], dim["thread_len"]
    Polyline(
        (0,      -length - tl),
        (d / 2,  -length - tl),
        (d / 2,  -length),
        (sd / 2, -length),
        (sd / 2,  0),
        (dk / 2,  0),
        (dk / 2,  k),
        (0,       k),
        close=True,
    )


_PROFILE_FNS = {
    "SHCS": _shcs_profile,
    "FHCS": _fhcs_profile,
    "BHCS": _bhcs_profile,
    "SHOULDER": _shoulder_profile,
}


# ── Geometry ──────────────────────────────────────────────────────────────────
class Screw(BasePart):
    """Metric socket screw (SHCS / FHCS / BHCS).

    Z = 0 sits at the underhead seating plane; head at +Z, shank at −Z so the
    part drops into an assembly hole naturally. `length` is underhead for
    SHCS/BHCS, overall for FHCS (head height is included)."""

    def __init__(
        self,
        screw_type: str,
        size: str,
        length: float,
        qty: int = 1,
    ):
        super().__init__(qty=qty)
        if screw_type not in TABLES:
            raise ValueError(
                f"Unknown screw_type {screw_type!r}; expected SHCS / FHCS / BHCS"
            )
        if size not in TABLES[screw_type]:
            raise ValueError(
                f"{screw_type} has no entry for size {size!r}; "
                f"available: {sorted(TABLES[screw_type])}"
            )
        k = TABLES[screw_type][size]["k"]
        if screw_type == "FHCS" and length <= k:
            raise ValueError(
                f"FHCS length ({length} mm) must exceed head height k = {k} mm"
            )
        self.screw_type = screw_type
        self.size = size
        self.length = length

    def name_suffix(self) -> str:
        return f"_{self.screw_type}_{self.size}x{self.length:g}_x{self.qty}"

    def _build(self):
        d = COMMON[self.size]["d"]
        dim = TABLES[self.screw_type][self.size]
        k, s = dim["k"], dim["s"]

        with BuildPart() as p:
            # Half-profile in Plane.XZ, revolved around the Z axis.
            with BuildSketch(Plane.XZ):
                with BuildLine():
                    _PROFILE_FNS[self.screw_type](d, self.length, dim)
                make_face()
            revolve(axis=Axis.Z)

            # Hex drive socket — sketched on Plane.XY at the head top and
            # extruded downward by sock_depth (Mode.SUBTRACT). For BHCS the
            # dome curves below z = k, so the prism's top portion is in air
            # and only the part inside the dome material is removed.
            sock_depth = SOCKET_DEPTH_FRAC[self.screw_type] * k
            hex_circumradius = s / (2 * math.cos(math.radians(30)))
            with BuildSketch(Plane.XY.offset(k)):
                RegularPolygon(radius=hex_circumradius, side_count=6)
            extrude(amount=-sock_depth, mode=Mode.SUBTRACT)

        return p.part
