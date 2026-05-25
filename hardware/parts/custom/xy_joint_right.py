from build123d import *

from hardware.parts.base import BaseCustomPart
from hardware.parts.custom.xy_joint_left import _build_shape


class XyJointRight(BaseCustomPart):
    def _build(self):
        return _build_shape().mirror(Plane.YZ)


if __name__ == "__main__":
    XyJointRight().export()
