"""Camera on the frame — mounts the camera_30 arm onto board_40_teflon's RIGHT
long extrusion, on its outward (+X) side face.

In phone_40 / board_40 world the two long 2040s run along Z at X = ±95; the
right long's outward side face is its +X face (at X = 105). That 40 mm face
carries two T-slots at world Y = ±10 (running along Z). The corner bracket is
installed HORIZONTALLY — its two hammer T-nuts (30 mm apart) drop into the same
slot, spaced along Z — so the gooseneck arm reaches out over the phone bed and
the camera looks down at it.

Placement (CAMERA_PLACEMENT) maps the bracket's local frame onto the face:
  bracket +Z (deck outward normal) → world +X   (deck seats on the +X face)
  bracket +Y (the two-hole line)   → world +Z   (the two T-nuts run along the slot)
  bracket +X (deck depth)          → world +Y
The deck hole at bracket x = 15 lands on the chosen slot (world Y = SLOT_Y); the
two holes straddle MOUNT_Z, 30 mm apart along Z.

Two variants:
  * exploded — the camera arm pulled out along +X off the side face.
  * assembled — the bracket seated on the face, T-nuts in the slot.

Run from the repo root:

    uv run --group cad python -m hardware.assembly.procedures.camera_40_frame
"""

from build123d import Compound, Location, Plane

from hardware.assembly.base import BaseAssembly
from hardware.assembly.procedures.board_40_teflon import BO40Teflon
from hardware.assembly.procedures.camera_30_mount import Camera30Mount
from hardware.assembly.projection import Camera, MAIN_FRAME_VIEW
from hardware.parts.standard.extrusion import leg
from hardware.parts.standard.t_nut import HAMMER_TOTAL_HEIGHT

# Right long 2040 — center at world X = +95 (phone_40 frame), 20 mm along X, so
# its outward +X side face sits at X = 95 + leg = 105. The 40 mm face has two
# slots at world Y = ±10.
LONG_CENTER_X = 95
FACE_X        = LONG_CENTER_X + leg    # = 105, outward +X side face
SLOT_Y        = 10                     # the second (lower, +Y / bed-side) slot
MOUNT_Z       = 147.5                  # along the long, at the bed's mid-length
DECK_HOLE_X   = 15                     # bracket-local X of the two deck holes
EXPLODE       = 70                     # mm — exploded: arm pulled out +X off the face

# Bracket frame → extrusion face. x_dir = image of bracket +X, z_dir = image of
# bracket +Z; the origin offsets bracket x=DECK_HOLE_X onto the slot (Y=SLOT_Y)
# and the two holes (bracket y=±15) onto MOUNT_Z ± 15 along Z.
#
# The bracket's deck is lifted HAMMER_TOTAL_HEIGHT off its own z=0 (it sits on
# the T-nut bosses in camera_10), so the deck-bottom — the L-face that must seat
# flat on the extrusion side face — is at bracket z = HAMMER_TOTAL_HEIGHT. Pull
# the origin in by that amount so the deck contacts the face at X = FACE_X and
# the T-nuts drop INTO the slot (X < FACE_X) instead of standing proud of it.
CAMERA_PLACEMENT = Plane(
    origin=(FACE_X - HAMMER_TOTAL_HEIGHT, SLOT_Y - DECK_HOLE_X, MOUNT_Z),
    x_dir=(0, 1, 0),    # bracket +X → world +Y
    z_dir=(1, 0, 0),    # bracket +Z → world +X
)


class Camera40Frame(BaseAssembly):
    compound_label = "camera_40_frame"
    camera = [MAIN_FRAME_VIEW, Camera(35.39, -56.69, 45.46)]

    def _build(self) -> Compound:
        base = BO40Teflon(exploded=False).build()

        cam = Camera30Mount(exploded=False).build()
        cam.move(Location(CAMERA_PLACEMENT))
        if self.exploded:
            cam.move(Location((EXPLODE, 0, 0)))

        return Compound(label=self.compound_label, children=[base, cam])


if __name__ == "__main__":
    for exploded in (True, False):
        asm = Camera40Frame(exploded=exploded)
        asm.export()
        asm.render()
