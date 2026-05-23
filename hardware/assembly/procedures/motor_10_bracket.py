"""Motor + bracket fastening — motor bracket plate attached to a
NEMA 17 motor via 4 BHCS M3 in the motor's top-face threaded
mounts, plus 2 BHCS M5 and 2 ring spacers that form the bracket's
frame-mount interface on the +X side.

Two variants:

  * exploded — motor at the bottom, bracket above with BRACKET_GAP
               of air, all 6 screws above with SCREW_GAP air below
               their shank tips (shared shank-tip line, so M5 heads
               sit higher than M3 heads by their length difference).
               Rings hang from the bracket bottom — they're a
               bracket-side feature, travelling with the bracket.
  * assembled — finished state: bracket bottom flush on the motor's
                top body face, 4 BHCS M3 dropped through the bracket
                into the motor's M3 mounts; 2 BHCS M5 dropped through
                the bracket and the ring spacers, ready to thread
                into a frame hammer t-nut (frame not modelled here).

The bracket's 25 mm shaft pass-through sits at bracket-local
X = -10 mm. To centre the pass-through over the motor shaft (at
world 0, 0), the bracket is translated +10 mm along world X. The
four M3 holes then land at world (±half_pitch, ±half_pitch),
directly over the motor's M3 mounts; the two M5 holes land at
world (+32, ±12.5), well clear of the motor body — that's where
the rings hang and the M5 screws drop through.

  * 1 x Nema17Motor
  * 1 x MotorBracket
  * 4 x BHCS M3 × 6 — into the motor's top-face M3 mounts
  * 2 x BHCS M5 × 20 — through the bracket and ring spacer
  * 2 x Ring M6 × 20 × 12 — spacer between bracket and frame

Run from the repo root:

    uv run --group cad python -m hardware.assembly.procedures.motor_10_bracket
"""

from build123d import Axis, Compound, Location

from hardware.assembly.base import BaseAssembly
from hardware.assembly.render import Camera
from hardware.parts.standard.bracket import (
    MotorBracket as MotorBracketPart,
    motor_m5_pitch,
    motor_m5_x_inset,
    motor_mount_pitch,
    motor_plate_length,
    motor_plate_thick,
    motor_shaft_x_offset,
)
from hardware.parts.standard.motor import Nema17Motor, default_height
from hardware.parts.standard.ring import SPECS as RING_SPECS, Ring
from hardware.parts.standard.screw import Screw

BHCS_M3_LENGTH = 6           # mm — BHCS M3 underhead length (motor mount)
BHCS_M5_LENGTH = 20          # mm — BHCS M5 underhead length (frame mount)
RING_SPEC      = "M6x20x12"  # 20 mm OD × 12 mm tall spacer (M6 bore)
BRACKET_GAP    = 30          # mm — exploded: motor top face → bracket bottom
SCREW_GAP      = 15          # mm — exploded: bracket top → screw shank tip
RING_GAP       = 12          # mm — exploded: bracket bottom → ring top (drop
                             #      the rings clear of the bracket so the
                             #      spacer reads as a separate part)


class MO10Bracket(BaseAssembly):
    # Subclasses share this build logic and only override the two
    # class attributes below — ``compound_label`` retargets the
    # STEP / SVG filename, and ``motor_z_rotation`` flips the motor
    # about Z to swap which side the cable connector ends up on once
    # the bracket is composed onto a frame. ``_module_stem()`` already
    # derives the output filename from the subclass's own module, so
    # no other override is needed.
    compound_label: str = "motor_10_bracket"
    motor_z_rotation: float = 180   # 180° puts the plug on native +Y → world
                                    # -X (LEFT side from top view) when
                                    # placed via motor_11_frame's mapping.
    camera = Camera(-30, 25)

    def _build(self) -> Compound:
        motor = Nema17Motor().build().rotate(Axis.Z, self.motor_z_rotation)
        bracket = MotorBracketPart().build()
        screws_m3 = [Screw("BHCS", "M3", BHCS_M3_LENGTH).build() for _ in range(4)]
        screws_m5 = [Screw("BHCS", "M5", BHCS_M5_LENGTH).build() for _ in range(2)]
        rings = [Ring(RING_SPEC).build() for _ in range(2)]
        ring_height = RING_SPECS[RING_SPEC]["height"]

        # Motor: centered at origin; body top face at z = +height/2.
        # M3 threaded mounts sit at world (±half_pitch, ±half_pitch,
        # motor_top_z), 4 mm deep into the body.
        motor_top_z = default_height / 2

        # Bracket translation: the shaft hole sits at bracket-local
        # x = -plate_length/2 + shaft_offset (= -10). Shift the bracket
        # by the negative of that so the shaft hole lands at world
        # (0, 0) — directly over the motor shaft. The four bracket M3
        # holes (bracket-local at the shaft hole ± mount_pitch/2) then
        # land at world (±half_pitch, ±half_pitch), over the motor's
        # mounts. The two M5 holes land at world (m5_world_x, ±half_m5).
        bracket_dx = motor_plate_length / 2 - motor_shaft_x_offset
        half_pitch = motor_mount_pitch / 2
        m5_world_x = bracket_dx + motor_plate_length / 2 - motor_m5_x_inset
        half_m5    = motor_m5_pitch / 2

        # Layout per variant:
        #   exploded:  bracket lifted BRACKET_GAP above the motor top
        #              face; all 6 screw shank tips float SCREW_GAP
        #              above the bracket top (shared shank-tip line —
        #              M5 heads end up higher than M3 heads by the
        #              length difference, which reads as "longer screws
        #              for the longer reach into the frame").
        #   assembled: bracket bottom flush with motor top body face;
        #              each BHCS button-head bottom flat (z=0 in the
        #              screw's local frame) contacts the bracket top —
        #              underhead Z = bracket_top_z regardless of length.
        # Rings hang from the bracket bottom in BOTH variants — they're
        # a bracket-side feature whose mate is the frame, not present
        # in this assembly, so we anchor them to the bracket instead.
        bracket_bottom_z = motor_top_z + (BRACKET_GAP if self.exploded else 0)
        bracket_top_z    = bracket_bottom_z + motor_plate_thick
        if self.exploded:
            shank_tip_z = bracket_top_z + SCREW_GAP
            m3_under_z  = shank_tip_z + BHCS_M3_LENGTH
            m5_under_z  = shank_tip_z + BHCS_M5_LENGTH
        else:
            m3_under_z = m5_under_z = bracket_top_z
        bracket.move(Location((bracket_dx, 0, (bracket_top_z + bracket_bottom_z) / 2)))

        # M3 screws — 4 at (±half_pitch, ±half_pitch). Identity
        # rotation: head at +Z, shank at -Z.
        m3_positions = [
            (-half_pitch, -half_pitch),
            (-half_pitch,  half_pitch),
            ( half_pitch, -half_pitch),
            ( half_pitch,  half_pitch),
        ]
        for screw, (sx, sy) in zip(screws_m3, m3_positions):
            screw.move(Location((sx, sy, m3_under_z)))

        # M5 screws — 2 at (m5_world_x, ±half_m5).
        m5_positions = [(m5_world_x, -half_m5), (m5_world_x, half_m5)]
        for screw, (sx, sy) in zip(screws_m5, m5_positions):
            screw.move(Location((sx, sy, m5_under_z)))

        # Rings: bore axis at each M5 hole's world XY. In assembled,
        # top of ring sits on bracket bottom. In exploded, drop the
        # ring by RING_GAP so the spacer reads as a distinct part
        # rather than blending into the bracket bottom edge.
        ring_top_z = bracket_bottom_z - (RING_GAP if self.exploded else 0)
        for ring, (rx, ry) in zip(rings, m5_positions):
            ring.move(Location((rx, ry, ring_top_z - ring_height)))

        # Hooks for a downstream frame composition to flush-mount this
        # sub-assembly without re-deriving internal bracket geometry:
        #   * bracket_bottom_z / bracket_top_z — native Z of the
        #     bracket's bottom / top face (bottom matches
        #     FR30BracketTnut.bracket_bottom_z). bracket_top_z is the
        #     seat where a pulley on the shaft would sit.
        #   * m5_native_x     — native X of the M5 hole pair.
        #   * ring_height     — height of the ring spacer that the
        #                       frame composition uses to gauge the
        #                       bracket-to-slot-face offset.
        self.bracket_bottom_z = bracket_bottom_z
        self.bracket_top_z    = bracket_top_z
        self.m5_native_x      = m5_world_x
        self.ring_height      = ring_height

        return Compound(label=self.compound_label, children=[
            motor, bracket, *screws_m3, *screws_m5, *rings,
        ])


if __name__ == "__main__":
    for exploded in (True, False):
        asm = MO10Bracket(exploded=exploded)
        asm.export()
        asm.render()
