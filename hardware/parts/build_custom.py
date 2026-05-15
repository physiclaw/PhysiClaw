"""Build every custom part — one .step file per part written to
hardware/output/step/. Run: `uv run python -m parts.build_custom`."""

from hardware.parts.base import build_all
from hardware.parts.custom.belt_clamp import BeltClamp
from hardware.parts.custom.pulley_mount_front import PulleyMountFront
from hardware.parts.custom.pulley_mount_motor import PulleyMountMotor
from hardware.parts.custom.solenoid_mount import SolenoidMount
from hardware.parts.custom.xy_joint_left import XyJointLeft
from hardware.parts.custom.xy_joint_right import XyJointRight

ALL_PARTS = [
    BeltClamp(qty=1),
    PulleyMountFront(qty=2),
    PulleyMountMotor(qty=2),
    SolenoidMount(qty=1),
    XyJointLeft(qty=1),
    XyJointRight(qty=1),
]


if __name__ == "__main__":
    build_all(ALL_PARTS)
