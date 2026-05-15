"""Build every standard part — one .step file per part written to
hardware/output/step/. Run: `uv run python -m parts.build_standard`."""

from build123d import MM

from hardware.parts.base import build_all
from hardware.parts.standard.extrusion2020 import Extrusion2020
from hardware.parts.standard.nema17motor import Nema17Motor

ALL_PARTS = [
    Extrusion2020(length=200 * MM, qty=2),
    Extrusion2020(length=100 * MM, qty=2),
    Nema17Motor(qty=2),
]


if __name__ == "__main__":
    build_all(ALL_PARTS)
