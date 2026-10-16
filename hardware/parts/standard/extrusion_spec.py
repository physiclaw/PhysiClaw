"""The extrusion numbers, kept apart from the geometry — the standard-library
half of ``extrusion.py``.

Two readers share these constants. ``extrusion.py`` turns them into
build123d solids (it needs ``--group cad``); the sourcing drawing
(``hardware.documents.drawing.extrusion``) turns the same numbers into
the cut-and-drill sheet a supplier machines from, without loading the CAD
kernel. Keeping them here — plain floats in millimetres, no build123d
import — is what lets one edit move both the model and the drawing.

The 2020 cell, the 2040 channel and counterbores, and the 1020 profile
and end holes are all defined here; ``extrusion.py`` re-exports every
name, so its importers (the assembly procedures) are unchanged.
"""

from __future__ import annotations

from hardware.parts._fits import M6_NORMAL

# ── Shared 2020-cell parameters ───────────────────────────────────────────────
leg = 10.0  # outer half-extent of one 2020 cell
default_length = 200.0  # default extrusion length (override per-instance)
bore_diameter = 5.0  # center through-hole of each cell
corner_fillet = 1.5  # outer vertical corner edges
rib_fillet = 2.0  # inner rib vertical edges near the slot/diagonal joint

# 1/8 cross-section outline (one slot in profile). Traces, in order:
# center → bottom edge → slot notch → right edge → top corner → hypotenuse back.
# The solid is the region between this chain and the diagonal; the T-slot
# is the void between the chain and the axis (mouth at the face x = leg,
# lip behind it, the wide cavity, then the rib sloping back to the core).
wedge_vertices: tuple[tuple[float, float], ...] = (
    (0.0, 0.0),
    (3.9, 0.0),
    (3.9, 2.84),
    (6.56, 5.5),
    (8.2, 5.5),
    (8.2, 3.1),
    (9.5, 3.1),
    (9.5, 3.6),
    (10.0, 3.6),
    (10.0, 10.0),
)

# ── 2040-specific parameters ──────────────────────────────────────────────────
cell_offset = leg  # 2040 cell centers at ±10 mm along X
slot_w = 6.0  # central through-channel width (X)
slot_h = 16.4  # central through-channel height (Y)
slot_lip_under_y = 8.2  # cavity belly top, cell-local — T-nut wings
# seat here against the slot lip underside
slot_depth = leg - wedge_vertices[1][0]  # slot face → cavity floor (6.1):
# a screw through a rail or plate on the face must end above this

# The screw that closes the frame: an SHCS M6 through each long member's
# counterbore into the short member's tapped end bore (frame_20_SHCS).
frame_screw = "M6"

# End-counterbore screw access on the +Y (front) face, mirrored on each
# Z end. Two per end, aligned in X with the bores at ±cell_offset. Sized
# for the frame screw by DIN 974-1 (counterbores for ISO 4762 socket head
# cap screws), medium series — a supplier reads "M6 counterbore" as
# exactly these numbers:
#   through hole  6.6  ISO 273 medium clearance for M6 (the _fits table)
#   pocket Ø      11   for the M6 head (dk 10, screw.SHCS_DIMS)
#   pocket depth  6.8  DIN 974-1 t for M6 (head k 6.0 fully recessed)
cb_end_offset = 10.0  # axial offset of CB from each end face
cb_head_d = 11.0  # counterbore (head pocket) diameter
cb_head_depth = 6.8  # counterbore depth
cb_shaft_d = M6_NORMAL  # through-hole diameter

# The short 2040s take the frame screw in their end-cell bores, so their
# two bores are tapped at each end. Not modelled — a thread is a supplier
# operation — but the drawing calls it out in full:
#   the profile's Ø5 centre bore (bore_diameter) is the M6×1 tap drill
#   (major 6 − pitch 1), so the vendor taps the bore as it is;
#   pitch 1.0 = ISO 261 coarse, the default when a callout says just "M6";
#   thread depth 12 = 2 × d, the conservative engagement rule for aluminum
#   (1.5 d minimum), and well past the ~6.8 mm the frame screw reaches
#   through the long member's counterbored 20 mm wall.
end_tap = frame_screw
end_tap_pitch = 1.0
end_tap_class = "6H"  # ISO 965 medium internal-thread tolerance, the default
end_tap_depth = 12.0

# Joint labels for the four end counterbores on a 2040 with cb=True.
# Shared so callers can iterate without restating the names (typo risk).
CB_LABELS = (
    "cb_bot_left",
    "cb_bot_right",
    "cb_top_left",
    "cb_top_right",
)

# ── 1020-specific parameters ──────────────────────────────────────────────────
# Half cross-section outline (right half, x ≥ 0; mirrored across the Y axis
# for the full profile). Traces, in order:
# centerline bottom → bottom-right → top-right → top edge to slot lip →
# lip underside → cavity belly → rib slope → centerline → close.
half_vertices_1020: tuple[tuple[float, float], ...] = (
    (0.0, 0.0),
    (9.9, 0.0),
    (9.9, 9.9),
    (3.5, 9.9),
    (3.5, 9.4),
    (3.2, 9.4),
    (3.2, 8.0),
    (5.6, 8.0),
    (5.6, 6.4),
    (2.4, 3.6),
    (0.0, 3.6),
)
half_x_1020 = 9.9  # half cross-section width (= section height too)

# The profiles' nominal sizes — what their designations mean and what a
# supplier's stock is sold as. The 2040 model is exactly nominal; the 1020
# is modelled at its measured 19.8 × 9.9 so the parts that seat on it fit,
# but a drawing dimensions a purchased standard section by its nominal
# size, never by the model's measurement.
nominal_2040 = (40.0, 20.0)  # width × height
nominal_1020 = (20.0, 10.0)
hole_1020_d = 4.2  # through-hole diameter
hole_1020_x_inset = 3.0  # hole center inset from right edge
hole_1020_y_inset = 3.0  # hole center inset from bottom edge

# Optional end-mounting holes (Extrusion1020(hole=True)): one M5 clearance
# hole drilled vertically (through Y) at the section center, set in from each
# end face — a bolt passes up through the bottom into the T-slot.
end_hole_d = 5.5  # M5 clearance
end_hole_offset = 10.0  # hole center from each end face, along Z
