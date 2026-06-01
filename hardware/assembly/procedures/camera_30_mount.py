"""Camera mount — camera_20_gooseneck with the Camera part clipped onto the
gooseneck's male 1/4-20 stud.

Takes the gooseneck sub-assembly from camera_20 (corner bracket + frame-side
fasteners + the 1/4-20 screw + gooseneck) and seats the Camera on the free end
of the gooseneck: the camera's front-face 1/4-20 FEMALE socket receives the
gooseneck's MALE stud, so the camera hangs off the neck tip.

Variants:
  * exploded — inherits camera_20's exploded layout; the camera slides off the
    stud along the mount axis (CAM_MOUNT_GAP).
  * assembled — the camera's socket seated against the stud's collar face.

Parts (adds to camera_20_gooseneck):
  * 1 x Camera (82 × 26 × 25 mm)

Run from the repo root:

    uv run --group cad python -m hardware.assembly.procedures.camera_30_mount
"""

from build123d import Axis, Location, Plane

from hardware.assembly.procedures.camera_20_gooseneck import Camera20Gooseneck
from hardware.parts.standard.camera import Camera, height as CAM_H, width as CAM_W

CAM_MOUNT_GAP = 20   # mm — exploded: camera slid off the stud along the mount axis


class Camera30Mount(Camera20Gooseneck):
    compound_label = "camera_30_mount"

    def _parts(self) -> list:
        parts = super()._parts()   # bracket + gooseneck; sets self.stud_base / stud_axis

        cam = Camera().build()
        # Seat the camera's 1/4-20 female socket (the centre of its +Y face) on
        # the gooseneck's male stud. Map that local socket frame onto a world
        # frame at the stud base whose +Z opposes the stud axis, so the mouth
        # faces the stud and the body hangs off along the stud direction.
        socket = Plane(origin=(0, CAM_W / 2, CAM_H / 2), x_dir=(1, 0, 0), z_dir=(0, 1, 0))
        ax     = self.stud_axis
        gap    = CAM_MOUNT_GAP if self.exploded else 0
        origin = tuple(b + a * gap for b, a in zip(self.stud_base, ax))
        target = Plane(origin=origin, x_dir=(1, 0, 0), z_dir=tuple(-a for a in ax))
        cam.move(Location(target) * Location(socket).inverse())

        # Spin the camera 90° about the stud (mount) axis: the socket centre is
        # on the axis, so it stays seated while the camera's heading rotates.
        cam = cam.rotate(Axis(origin, ax), 90)

        return [*parts, cam]


if __name__ == "__main__":
    for exploded in (True, False):
        asm = Camera30Mount(exploded=exploded)
        asm.export()
        asm.render()
