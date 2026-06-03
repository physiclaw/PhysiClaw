"""Export every custom part — one .step file per part written to
hardware/output/step/. Run: `uv run python -m hardware.parts.export_custom`."""

from hardware.parts.base import export_all
from hardware.parts.custom.belt_clamp import BeltClamp
from hardware.parts.custom.idler_mount_front import IdlerMountFront
from hardware.parts.custom.idler_mount_motor import IdlerMountMotor
from hardware.parts.custom.solenoid_mount import SolenoidMount
from hardware.parts.custom.xy_joint_left import XyJointLeft
from hardware.parts.custom.xy_joint_right import XyJointRight

ALL_PARTS = [
    BeltClamp(qty=1),
    IdlerMountFront(qty=2),
    IdlerMountMotor(qty=2),
    SolenoidMount(qty=1),
    XyJointLeft(qty=1),
    XyJointRight(qty=1),
]


if __name__ == "__main__":
    export_all(ALL_PARTS)
