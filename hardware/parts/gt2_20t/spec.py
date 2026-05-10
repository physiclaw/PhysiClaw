"""Dimensions for the GT2 timing pulley, 20 teeth."""

from dataclasses import dataclass

from parts import Spec


@dataclass(frozen=True)
class GT220T(Spec):
    teeth: int
    pitch_mm: float
    outer_diameter_mm: float
    belt_width_mm: float
    bore_diameter_mm: float
    tooth_cavity_radius_mm: float


GT2_20T = GT220T(
    teeth=20,
    pitch_mm=2.0,
    outer_diameter_mm=12.2,
    belt_width_mm=6.0,
    bore_diameter_mm=5.0,
    tooth_cavity_radius_mm=0.5,
)
