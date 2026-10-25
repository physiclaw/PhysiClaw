"""Numeric guards shared by arm motion and solenoid contact.

Call these before serial I/O or energizing the coil. They validate the
command's numbers; calibration remains responsible for travel geometry.
"""

import math
from numbers import Real


def require_finite(**values: float) -> None:
    for name, value in values.items():
        try:
            valid = (
                not isinstance(value, bool)
                and isinstance(value, Real)
                and math.isfinite(value)
            )
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError(f"{name} must be a finite number")


def validate_move(x: float, y: float, speed: int) -> None:
    require_finite(x=x, y=y, speed=speed)
    if speed <= 0:
        raise ValueError("speed must be positive")


def validate_dwell(seconds: float) -> None:
    require_finite(seconds=seconds)
    if seconds < 0:
        raise ValueError("dwell must be non-negative")
