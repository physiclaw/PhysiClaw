"""Dimensions for the NEMA 17 stepper motor."""

from dataclasses import dataclass

from parts import Spec


@dataclass(frozen=True)
class Nema17(Spec):
    body_size_mm: float
    body_length_mm: float
    corner_radius_mm: float
    boss_diameter_mm: float
    boss_height_mm: float
    shaft_diameter_mm: float
    shaft_length_mm: float
    hole_spacing_mm: float
    hole_diameter_mm: float


NEMA17_SPEC = Nema17(
    body_size_mm=42.3,
    body_length_mm=40.0,
    corner_radius_mm=5.5,
    boss_diameter_mm=22.0,
    boss_height_mm=2.0,
    shaft_diameter_mm=5.0,
    shaft_length_mm=24.0,
    hole_spacing_mm=31.0,
    hole_diameter_mm=3.0,
)
