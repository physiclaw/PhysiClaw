"""Gantry travel and derived rail, frame, and phone-bed dimensions.

All dimensions are in millimetres. Gantry travel is fixed at 110 by 200 mm
and must match ``physiclaw.core.hardware.arm``.
CAD models and documents share the derived lengths below.
"""

X_TRAVEL = 110
Y_TRAVEL = 200

CARRIAGE_LENGTH = 40  # MGN9H footprint, rounded from 39.9 mm.
EXT_2040_WIDTH = 20  # Narrow face of the profile.

# End allowances include both ends of each rail.
X_BEAM_END_ALLOWANCE = 35
Y_EXTRUSION_END_ALLOWANCE = 105
X_EXTRUSION_CLEARANCE = 5

# Rail lengths.
X_RAIL_LENGTH = X_TRAVEL + CARRIAGE_LENGTH  # 150 mm
Y_RAIL_LENGTH = Y_TRAVEL + CARRIAGE_LENGTH  # 240 mm

# Gantry beam length.
X_BEAM_LENGTH = X_RAIL_LENGTH + X_BEAM_END_ALLOWANCE  # 185 mm

# Frame extrusion lengths.
X_EXTRUSION_LENGTH = X_BEAM_LENGTH + X_EXTRUSION_CLEARANCE  # 190 mm
Y_EXTRUSION_LENGTH = Y_RAIL_LENGTH + Y_EXTRUSION_END_ALLOWANCE  # 345 mm

# Phone-bed beam length.
PHONE_BED_BEAM_LENGTH = X_EXTRUSION_LENGTH + 2 * EXT_2040_WIDTH  # 230 mm
