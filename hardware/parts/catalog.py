"""Custom designs shared by individual exports and the 3D-print package.

This export inventory describes the nine CAD designs; the authored purchasing
list remains documents/bom/bom.json. Tests check that their quantities agree.
"""

from hardware.parts.custom.belt_clamp import BeltClamp
from hardware.parts.custom.idler_mount_front import IdlerMountFront
from hardware.parts.custom.idler_mount_motor import IdlerMountMotor
from hardware.parts.custom.pcb_holder import PcbHolder
from hardware.parts.custom.phone_bed import PhoneBed
from hardware.parts.custom.solenoid_mount import SolenoidMount
from hardware.parts.custom.tube_holder import TubeHolder
from hardware.parts.custom.xy_joint_left import XyJointLeft
from hardware.parts.custom.xy_joint_right import XyJointRight

# (class, qty to print, English name, 中文名). Quantities match the consolidated
# BOM's "Custom parts" line (11 pieces across 9 designs). The STEP filename comes
# from the part itself (BasePart.output_path()), so it isn't restated here.
CUSTOM_PARTS: list[tuple[type, int, str, str]] = [
    (BeltClamp, 1, "Belt clamp", "同步带夹具"),
    (IdlerMountFront, 2, "Idler mount (front)", "惰轮支座（前）"),
    (IdlerMountMotor, 2, "Idler mount (motor)", "惰轮支座（电机侧）"),
    (PcbHolder, 1, "Control board holder", "控制板支架"),
    (PhoneBed, 1, "Phone bed", "手机托架"),
    (SolenoidMount, 1, "Solenoid mount", "电磁铁支架"),
    (TubeHolder, 1, "Tube holder", "导管支架"),
    (XyJointLeft, 1, "XY joint (left)", "XY 连接件（左）"),
    (XyJointRight, 1, "XY joint (right)", "XY 连接件（右）"),
]
