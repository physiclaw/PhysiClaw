"""Camera gooseneck — camera_10_bracket with a flexible gooseneck mounted on
its 1/4-20 screw.

Takes the corner-bracket sub-assembly from camera_10 (bracket + frame-side
BHCS/T-nuts + the 1/4-20 SHCS and hex nut) and threads a Gooseneck onto that
screw by its FEMALE 1/4-20 socket: the socket mouth opens toward the stud and
captures the thread exposed past the hex nut, then the neck arcs up to a free
male 1/4-20 stud (where a camera mounts in a later step).

Variants:
  * exploded — inherits camera_10's exploded layout; the gooseneck slides off
    the stud along −X (GOOSE_GAP) so the female socket reads as separate.
  * assembled — the female socket seated on the stud against the hex nut.

Parts (adds to camera_10_bracket):
  * 1 x Gooseneck (Ø10 neck, 1/4-20 male + female ends)

Run from the repo root:

    uv run --group cad python -m hardware.assembly.procedures.camera_20_gooseneck
"""

from build123d import Axis, Compound

from hardware.assembly.procedures.camera_10_bracket import Camera10Bracket
from hardware.assembly.projection import FRONT_LEFT_HIGH
from hardware.parts.standard.gooseneck import Gooseneck, female_collar_len
from hardware.parts.standard.nut import SPECS as NUT_SPECS

NUT_THICK   = NUT_SPECS["hex"]["1/4-20"]["thickness"]   # mm — hex nut height
GOOSE_RISE  = 150   # mm — vertical span: male end height above the screw axis
GOOSE_REACH = 85    # mm — horizontal span: male end −X reach beyond the female end
GOOSE_GAP   = 16    # mm — exploded: gooseneck slid off the stud along −X


class Camera20Gooseneck(Camera10Bracket):
    compound_label = "camera_20_gooseneck"
    camera = FRONT_LEFT_HIGH

    def _build(self) -> Compound:
        parts = self._parts()   # bracket + frame fasteners + 1/4-20 screw + hex nut

        # The 1/4-20 screw protrudes on the −X face (head on +X). Thread the
        # female socket onto the exposed stud just past the hex nut: the mouth
        # opens +X (toward the stud) so the screw enters it; the neck then arcs
        # up to a free male stud. cam_offset / cam_y / cam_z come from _parts.
        nut_back_x = -self.cam_offset - NUT_THICK
        mouth_x    = nut_back_x - (GOOSE_GAP if self.exploded else 0)

        female_pt = (mouth_x - female_collar_len, self.cam_y, self.cam_z)
        male_pt   = (female_pt[0] - GOOSE_REACH, self.cam_y, self.cam_z + GOOSE_RISE)
        goose = Gooseneck(
            point1=male_pt,   direction1=(0, 0, -1),   # male stud points +Z (up)
            point2=female_pt, direction2=(1, 0, 0),    # female mouth opens +X onto the stud
        ).build()

        # Rotate the whole gooseneck 180° about the screw axis (world X
        # through the socket): the female end stays seated on the stud while
        # the neck swings to the opposite side (arm now descends instead of
        # rising).
        goose = goose.rotate(Axis((female_pt[0], self.cam_y, self.cam_z), (1, 0, 0)), 180)

        return Compound(label=self.compound_label, children=[*parts, goose])


if __name__ == "__main__":
    for exploded in (True, False):
        asm = Camera20Gooseneck(exploded=exploded)
        asm.export()
        asm.render()
