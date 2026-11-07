"""Single source of truth for the build's key size dimensions — X/Y gantry
travel, frame size, and the phone bed.

The two BASE knobs are ``X_TRAVEL`` and ``Y_TRAVEL`` (usable carriage /
gantry travel). Everything else is DERIVED from them:

    rail length       = travel + carriage footprint
    hosting extrusion = rail + end allowance (clearance to the end blocks)
    frame width       = X beam + clearance
    phone-bed beam    = frame width + two 2040 widths

So to change how far the machine reaches, edit ``X_TRAVEL`` / ``Y_TRAVEL``
here — the rail, frame (length and width), beam,
and phone-bed lengths follow, and the belt path, motor / idler / pulley-mount positions,
and T-nut seats all derive downstream and re-adapt on rebuild. (Note: X
travel widens the frame and phone bed; Y travel lengthens the frame.)
Document dimensions and rail fastener quantities resolve from these values
too; keep their named tokens in the authored document JSON.
"""

from hardware.scheme import MANUAL_ILLUSTRATIONS

# Keep CAD independent of the runtime. Tests check its matching travel limits.
X_TRAVEL = 110  # mm — toolhead carriage travel along the X crossbeam
Y_TRAVEL = 200  # mm — gantry travel along the long extrusions

# Manual crops and annotation coordinates were authored at these proportions.
# Only the isolated illustration subprocess uses them; documents use real travel.
ILLUSTRATION_TRAVEL = (110, 200)
if MANUAL_ILLUSTRATIONS:
    X_TRAVEL, Y_TRAVEL = ILLUSTRATION_TRAVEL

# ── Shared fixed allowances ──────────────────────────────────────────────────
CARRIAGE_LENGTH = 40  # mm — MGN9H slider footprint a rail loses to travel (≈ 39.9)
EXT_2040_WIDTH = 20  # mm — 2040 narrow-face width (= 2 × extrusion leg)

# ── X axis: travel → rail → crossbeam → frame width ──────────────────────────
X_BEAM_END_ALLOWANCE = 35  # mm — crossbeam overhang past the rail, out to the joints
X_EXTRUSION_CLEARANCE = 5  # mm — frame wider than the beam so it fits between the longs
X_RAIL_LENGTH = X_TRAVEL + CARRIAGE_LENGTH  # = 150  MGN9H X rail
X_BEAM_LENGTH = X_RAIL_LENGTH + X_BEAM_END_ALLOWANCE  # = 185  1020 crossbeam
X_EXTRUSION_LENGTH = (
    X_BEAM_LENGTH + X_EXTRUSION_CLEARANCE
)  # = 190  short 2040 (frame width)

# ── Y axis: travel → rail → long extrusion ───────────────────────────────────
Y_EXTRUSION_END_ALLOWANCE = (
    105  # mm — frame past the rail (end pulley-mount / idler blocks)
)
Y_RAIL_LENGTH = Y_TRAVEL + CARRIAGE_LENGTH  # = 240  MGN9H Y rail
Y_EXTRUSION_LENGTH = (
    Y_RAIL_LENGTH + Y_EXTRUSION_END_ALLOWANCE
)  # = 345  long 2040 (frame length)

# ── Phone bed (tracks X) ─────────────────────────────────────────────────────
# Cross-beam spans the frame width plus both long extrusions' widths.
PHONE_BED_BEAM_LENGTH = (
    X_EXTRUSION_LENGTH + 2 * EXT_2040_WIDTH
)  # = 230  1020 phone-bed beam
