"""Dimensions for the 2020 aluminum T-slot extrusion.

Cross-section constants (outer size, T-slot mouth/cavity, corner
fillet, etc.) are fixed by the 2020 profile and live as module
constants below — both backends import them so the FreeCAD and
build123d definitions can't drift on cross-section geometry. Only
`length_mm` and `bore_diameter_mm` are per-instance and live on
the dataclass.
"""

from dataclasses import dataclass

from parts import Spec

OUTER_SIZE_MM = 20.0          # outer square edge length
FILLET_RADIUS_MM = 1.5        # outer corner fillet radius
SLOT_MOUTH_MM = 6.2           # T-slot opening width
SLOT_CAVITY_MM = 11.0         # T-slot inner cavity width
SLOT_MOUTH_DEPTH_MM = 1.8     # mouth wall thickness
BORDER_TO_CENTER_MM = 6.1     # outer face → centre-block face
RIB_THICKNESS_MM = 1.5        # diagonal rib thickness


@dataclass(frozen=True)
class Extrusion2020(Spec):
    length_mm: float
    bore_diameter_mm: float


EXTRUSION_2020_L300 = Extrusion2020(
    length_mm=300.0,
    bore_diameter_mm=5.0,
)
