"""Build every standard part — one .step file per part written to
hardware/output/step/. Run: `uv run python -m parts.build_standard`."""

from build123d import MM

from hardware.parts.base import build_all
from hardware.parts.standard.extrusion2020 import Extrusion2020
from hardware.parts.standard.flat_bracket import FlatBracket
from hardware.parts.standard.hex_driver import HexDriver
from hardware.parts.standard.mgn9h_rail_slider import MGN9HRailSlider
from hardware.parts.standard.nema17motor import Nema17Motor
from hardware.parts.standard.pulley_2gt_20t import Pulley2GT20T
from hardware.parts.standard.ring import Ring
from hardware.parts.standard.screw import Screw

ALL_PARTS = [
    Extrusion2020(length=200 * MM, qty=2),
    Extrusion2020(length=100 * MM, qty=2),
    Nema17Motor(qty=2),
    FlatBracket(qty=6),
    Pulley2GT20T(toothed=True,  qty=2),
    Pulley2GT20T(toothed=False, qty=2),
    MGN9HRailSlider(rail_length=100 * MM, qty=2),
    Screw("BHCS",     "M3", 8  * MM),
    Screw("SHOULDER", "M4", 20 * MM),
    HexDriver("2mm", qty=1),
    Ring("M3x10x1"),
    Ring("M5x8x0.5"),
    Ring("M5x15x12"),
    Ring("M6x20x12"),
]


if __name__ == "__main__":
    build_all(ALL_PARTS)
