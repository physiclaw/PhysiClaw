"""Resolve authored document fields from the same dimensions as CAD.

Only named uppercase tokens are substituted; prose such as {inquiry} is
left for its own renderer. No expressions or code are evaluated.
"""

import re

from hardware.assembly import travel_ranges as travel
from hardware.documents import BuildError
from hardware.parts.standard import extrusion_spec

_TOKEN = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")


def dimension_values() -> dict[str, float]:
    # MGN9H mounting pitch in mm; travel consistency tests check these counts
    # against the CAD assemblies without making documents import the CAD kernel.
    hole_pitch = 20
    x_holes = max(1, int(travel.X_RAIL_LENGTH // hole_pitch))
    y_fasteners = 2 * max(1, int(travel.Y_RAIL_LENGTH // hole_pitch))
    return {
        "X_TRAVEL": travel.X_TRAVEL,
        "Y_TRAVEL": travel.Y_TRAVEL,
        "X_RAIL_LENGTH": travel.X_RAIL_LENGTH,
        "Y_RAIL_LENGTH": travel.Y_RAIL_LENGTH,
        "X_BEAM_LENGTH": travel.X_BEAM_LENGTH,
        "X_EXTRUSION_LENGTH": travel.X_EXTRUSION_LENGTH,
        "Y_EXTRUSION_LENGTH": travel.Y_EXTRUSION_LENGTH,
        "PHONE_BED_BEAM_LENGTH": travel.PHONE_BED_BEAM_LENGTH,
        "X_RAIL_FASTENERS": x_holes,
        "Y_RAIL_FASTENERS": y_fasteners,
        # The authored BOM also includes eight fixed M3x8 screws (belt clamp
        # and solenoid mount) and four fixed M3 drop-in nuts (phone bed).
        "BHCS_M3X8_QTY": x_holes + y_fasteners + 8,
        "TNUT_DROPIN_M3_QTY": x_holes + y_fasteners + 4,
        "Y_EXTRUSION_FAR_HOLE": travel.Y_EXTRUSION_LENGTH
        - extrusion_spec.cb_end_offset,
        "PHONE_BED_FAR_HOLE": travel.PHONE_BED_BEAM_LENGTH
        - extrusion_spec.end_hole_offset,
    }


def resolve_dimensions(data, *, source: str):
    """Return resolved JSON data without changing the authored templates."""
    values = dimension_values()

    def replace(match: re.Match) -> str:
        name = match[1]
        if name not in values:
            raise BuildError(f"{source}: unknown hardware dimension {{{name}}}")
        return f"{values[name]:g}"

    def walk(value):
        if isinstance(value, str):
            return _TOKEN.sub(replace, value)
        if isinstance(value, list):
            return [walk(item) for item in value]
        if isinstance(value, dict):
            return {key: walk(item) for key, item in value.items()}
        return value

    return walk(data)
