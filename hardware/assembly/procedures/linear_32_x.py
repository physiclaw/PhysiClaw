"""Linear X rail sub-assembly (short) — MGN9H 130 mm guideway with
M3 × 10 FHCS in the rail's mounting holes and hammer M3 T-nuts
hanging loosely from each shank tip, ready to engage.

Same construction as linear_10_y (LI10Y) — only the rail length,
screw-hole pattern, and slider position differ. The build logic is
reused via inheritance; this class just overrides four class
attributes.

Hole layout (6 holes at 20 mm pitch on a 130 mm rail):
  Screws at 1-indexed hole positions 1, 3, 4, 6 — both ends plus
  the central pair. Symmetric, 4 screws total at 40 mm / 20 mm /
  40 mm spacing (= LI10Y's 40 mm pitch maintained except for the
  shorter central span).

Slider position:
  slider_position = 0.5 puts the slider centered along the rail
  (= world X = 0 after LI33X places the rail centered on world X = 0),
  matching the X-axis carriage's natural home position.

See linear_10_y for the full part list, geometry derivation, and
variant descriptions.

Run from the repo root:

    uv run --group cad python -m hardware.assembly.procedures.linear_32_x
"""

from hardware.assembly.procedures.linear_10_y import LI10Y


class LI32X(LI10Y):
    compound_label     = "linear_32_x"
    rail_length        = 130
    screw_hole_indices = (1, 3, 4, 6)
    slider_position    = 0.5    # slider centered along the rail


if __name__ == "__main__":
    for exploded in (True, False):
        asm = LI32X(exploded=exploded)
        asm.export()
        asm.render()
