"""Export every standard part — one .step file per part written to
hardware/output/step/. Run: `uv run python -m hardware.parts.export_standard`."""

from build123d import MM

from hardware.parts.base import export_all
from hardware.parts.standard.bracket import FlatBracket
from hardware.parts.standard.bumper import Bumper
from hardware.parts.standard.driver import HexDriver
from hardware.parts.standard.extrusion import Extrusion2020
from hardware.parts.standard.mgn9h import MGN9H
from hardware.parts.standard.motor import Nema17Motor
from hardware.parts.standard.nut import Nut
from hardware.parts.standard.pulley import Pulley2GT20T
from hardware.parts.standard.ring import Ring
from hardware.parts.standard.screw import Screw
from hardware.parts.standard.solenoid import Solenoid
from hardware.parts.standard.t_nut import TNut
from hardware.parts.standard.tip import Tip

ALL_PARTS = [
    Extrusion2020(length=200 * MM, qty=2),
    Extrusion2020(length=100 * MM, qty=2),
    Nema17Motor(qty=2),
    FlatBracket(qty=6),
    Pulley2GT20T(kind="pulley", qty=2),
    Pulley2GT20T(kind="idler",  toothed=True,  qty=2),
    Pulley2GT20T(kind="idler",  toothed=False, qty=2),
    MGN9H(rail_length=150 * MM, qty=2),
    Screw("BHCS",     "M3", 8  * MM),
    Screw("SHOULDER", "M4", 20 * MM),
    HexDriver("2mm", qty=1),
    Ring("M3x10x1"),
    Ring("M5x8x0.5"),
    Ring("M5x15x12"),
    Ring("M6x20x12"),
    Nut("hex", "M3"),
    Nut("hex", "M4"),
    Nut("hex", "M5"),
    Nut("square", "M3"),
    Nut("square", "M4"),
    Nut("square", "M5"),
    TNut("standard", "M3"),
    TNut("standard", "M4"),
    TNut("standard", "M5"),
    TNut("hammer",   "M3"),
    TNut("hammer",   "M4"),
    TNut("hammer",   "M5"),
    Solenoid(),
    Bumper(),
    Tip(),
]


if __name__ == "__main__":
    export_all(ALL_PARTS)
