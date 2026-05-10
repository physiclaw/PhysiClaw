"""Dimensions for the 608ZZ deep-groove ball bearing."""

from dataclasses import dataclass

from parts import Spec


@dataclass(frozen=True)
class Bearing608(Spec):
    inner_diameter_mm: float
    outer_diameter_mm: float
    width_mm: float


BEARING_608 = Bearing608(
    inner_diameter_mm=8.0,
    outer_diameter_mm=22.0,
    width_mm=7.0,
)
