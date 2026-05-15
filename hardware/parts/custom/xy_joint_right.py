from build123d import *

from hardware.parts.base import BasePart
from hardware.parts.custom.xy_joint_left import _build_shape


class XyJointRight(BasePart):
    def _build(self):
        return _build_shape().mirror(Plane.YZ)


if __name__ == "__main__":
    XyJointRight().build()
