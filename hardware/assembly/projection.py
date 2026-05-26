import math
from dataclasses import dataclass, replace

from build123d import Shape, Vector


@dataclass(frozen=True)
class Camera:
    """Camera direction (degrees) for project_to_viewport.

    Conventions:
      * Z is up in world coords.
      * azimuth: 0 = front (-Y), 90 = right (+X), -90 = left, 180 = back.
      * elevation: 0 = horizontal, 90 = top-down.
      * roll: roll around the view axis. Positive = object rotates
        clockwise on paper. 0 = world +Z projected as up.
    """
    azimuth: float
    elevation: float
    roll: float = 0.0

    def __mul__(self, distance: float) -> Vector:
        """Camera origin scaled to `distance` from world origin."""
        az = math.radians(self.azimuth)
        el = math.radians(self.elevation)
        horizontal = math.cos(el)
        return Vector(
             horizontal * math.sin(az) * distance,
            -horizontal * math.cos(az) * distance,
             math.sin(el) * distance,
        )

    __rmul__ = __mul__

    def rotate(self, angle: float) -> "Camera":
        """Return a copy with ``roll`` set to ``angle`` (degrees)."""
        return replace(self, roll=angle)

    @property
    def up(self) -> Vector:
        """Up vector for project_to_viewport, rolled by ``self.roll``."""
        z = Vector(0, 0, 1)
        view_dir = (self * -1.0).normalized()
        base = (z - view_dir * view_dir.dot(z)).normalized()
        # Negative angle so positive roll rotates the object clockwise on
        # paper. Skip the rotation at 0 for float stability (Rodrigues with
        # θ=0 is mathematically identity but introduces cross-product noise).
        if self.roll == 0:
            return base
        theta = -math.radians(self.roll)
        # Rodrigues simplifies — base ⊥ view_dir means the parallel term is 0.
        return base * math.cos(theta) + view_dir.cross(base) * math.sin(theta)


FRONT = Camera(0, 0)
RIGHT = Camera(90, 0)
TOP   = Camera(0, 90)
ISO   = Camera(45, 35.2644)

# Procedure-view presets. Naming encodes azimuth / elevation / non-zero
# roll so the angle is readable at the import site. `MAIN_FRAME_VIEW` is
# the one exception — kept semantic because the angle is calibrated to
# the X-rail bbox.
FRONT_LEFT_HIGH    = Camera(-30,  25)
FRONT_RIGHT_HIGH   = Camera( 30,  25)
FRONT_LEFT_LOW     = Camera(-30, -20)
BACK_RIGHT_HIGH    = Camera(150,  25)
BACK_RIGHT_LOW_R90 = Camera(120, -20, 90)
FRONT_LEFT_LOW_R70 = Camera(-45, -20, 70)
FRONT_HIGH_L10     = Camera( 15,  45, -10)
MAIN_FRAME_VIEW    = Camera( -15, -15, -3)

def camera_view(
    shape: Shape,
    camera: Camera,
    *,
    distance_factor: float = 4,
) -> tuple[Vector, Vector, Vector]:
    """Camera position + up + look_at for projecting `shape` from `camera`.

    Camera position = ``shape.bounding_box().center() + camera * (reach * distance_factor)``
    where ``reach`` is the part's farthest extent from world origin.
    ``distance_factor=4`` leaves noticeable perspective foreshortening;
    raise to push toward orthographic.

    Returned ``look_at`` is the bbox center; pass it (along with pos / up)
    to ``project_to_viewport`` when projecting subsets that must align —
    otherwise project_to_viewport defaults look_at to each subset's own
    center, which warps the projection direction per subset.
    """
    bbox = shape.bounding_box()
    reach = max(abs(v) for v in (
        bbox.min.X, bbox.max.X, bbox.min.Y, bbox.max.Y, bbox.min.Z, bbox.max.Z,
    ))
    look_at = bbox.center()
    return look_at + camera * (reach * distance_factor), camera.up, look_at
