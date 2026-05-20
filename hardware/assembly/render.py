import math
from dataclasses import dataclass, replace

from build123d import Vector


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

    def __mul__(self, distance: float) -> tuple[float, float, float]:
        """Camera origin scaled to `distance` from world origin."""
        az = math.radians(self.azimuth)
        el = math.radians(self.elevation)
        horizontal = math.cos(el)
        return (
             horizontal * math.sin(az) * distance,
            -horizontal * math.cos(az) * distance,
             math.sin(el) * distance,
        )

    __rmul__ = __mul__

    def rotate(self, angle: float) -> "Camera":
        """Return a copy with ``roll`` set to ``angle`` (degrees)."""
        return replace(self, roll=angle)

    @property
    def up(self) -> tuple[float, float, float]:
        """Up vector for project_to_viewport, rolled by ``self.roll``."""
        z = Vector(0, 0, 1)
        view_dir = Vector(*(self * -1.0)).normalized()
        base = (z - view_dir * view_dir.dot(z)).normalized()
        # Negative angle so positive roll rotates the object clockwise on
        # paper. Skip the rotation at 0 for float stability (Rodrigues with
        # θ=0 is mathematically identity but introduces cross-product noise).
        if self.roll == 0:
            return (base.X, base.Y, base.Z)
        theta = -math.radians(self.roll)
        # Rodrigues simplifies — base ⊥ view_dir means the parallel term is 0.
        rolled = base * math.cos(theta) + view_dir.cross(base) * math.sin(theta)
        return (rolled.X, rolled.Y, rolled.Z)


FRONT = Camera(0, 0)
RIGHT = Camera(90, 0)
TOP   = Camera(0, 90)
# True iso: camera looks along (1,1,1)/√3 so X, Y, Z foreshorten equally.
ISO   = Camera(45, math.degrees(math.atan(1 / math.sqrt(2))))


def project(shape, camera: Camera, *, distance_factor: float = 4):
    """Project `shape` from `camera`. Returns ``(visible, hidden)`` edges.

    Camera position = ``shape.bounding_box().center() + camera * (reach * distance_factor)``
    where ``reach`` is the part's farthest extent from world origin.
    ``distance_factor=4`` leaves noticeable perspective foreshortening;
    raise to push toward orthographic.
    """
    bbox = shape.bounding_box()
    reach = max(abs(v) for v in (
        bbox.min.X, bbox.max.X, bbox.min.Y, bbox.max.Y, bbox.min.Z, bbox.max.Z,
    ))
    pos = bbox.center() + camera * (reach * distance_factor)
    return shape.project_to_viewport(pos, camera.up)