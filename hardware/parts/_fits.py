"""ISO 273 metric screw clearance hole diameters.

Three fit classes:
  CLOSE  — tight; better alignment, harder assembly.
  NORMAL — general purpose; default for most mounting.
  LOOSE  — extra clearance; tolerates misalignment.

For FDM-printed parts, real holes print 0.1-0.3 mm undersized. Bump up
by ~0.2 mm above NORMAL, or use LOOSE, when the target is FDM rather
than machined. Test-print a tolerance gauge if unsure.
"""
from build123d import MM

#                              CLOSE     NORMAL    LOOSE
M3_CLOSE, M3_NORMAL, M3_LOOSE = 3.2 * MM, 3.4 * MM, 3.6 * MM
M4_CLOSE, M4_NORMAL, M4_LOOSE = 4.3 * MM, 4.5 * MM, 4.8 * MM
M5_CLOSE, M5_NORMAL, M5_LOOSE = 5.3 * MM, 5.5 * MM, 5.8 * MM


# ── Square nut nominal dimensions (DIN 557) ───────────────────────────────────
# W = width across flats (side of the square)
# T = thickness (axial dimension)
# For 3D-printed nut sockets, add ~0.2 mm clearance on each dimension so the
# nut slides in.

#                    W         T
M3_NUT_W, M3_NUT_T = 5.7 * MM, 2.7 * MM
M4_NUT_W, M4_NUT_T = 7.3 * MM, 3.3 * MM
M5_NUT_W, M5_NUT_T = 8.3 * MM, 4.0 * MM
