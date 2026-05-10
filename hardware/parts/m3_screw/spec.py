"""Dimensions for the ISO 4762 socket-head cap screw, M3."""

from dataclasses import dataclass

from parts import Spec


@dataclass(frozen=True)
class M3Screw(Spec):
    head_diameter_mm: float
    head_height_mm: float
    shaft_diameter_mm: float
    shaft_length_mm: float
    hex_across_flats_mm: float
    hex_depth_mm: float


M3_SCREW_X10 = M3Screw(
    head_diameter_mm=5.5,
    head_height_mm=3.0,
    shaft_diameter_mm=3.0,
    shaft_length_mm=10.0,
    hex_across_flats_mm=2.5,
    hex_depth_mm=2.0,
)
