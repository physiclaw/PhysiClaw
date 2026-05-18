from build123d import *

from hardware.parts.base import BasePart

# ── Slider (block) parameters ────────────────────────────────────────────────
# MGN9H linear-rail carriage (Hiwin-style, 9 mm rail, "H" = long variant).
block_length   = 39.9 * MM    # X, along the rail
block_width    = 20   * MM    # Y, across the rail
block_thick    =  8   * MM    # Z, top face above rail-engagement face

# 4× M3 tapped mounting holes on the top face, 3 mm deep.
mount_hole_dia   =  3 * MM
mount_hole_depth =  3 * MM
mount_pitch_x    = 16 * MM    # along the rail
mount_pitch_y    = 15 * MM    # across the rail

# Rail-engagement groove on the bottom (simplified rectangular slot).
rail_groove_w     =  9.5 * MM    # 9 mm rail + clearance
rail_groove_depth =  4.5 * MM    # deep enough so block top is 10 mm above
                                 # rail base (6.5 + 8 - 4.5 = 10)

# ── Rail parameters ──────────────────────────────────────────────────────────
rail_width           =  9   * MM   # MGN9 standard
rail_height          =  6.5 * MM   # MGN9 standard rail height
rail_hole_dia        =  3.5 * MM   # M3 clearance through-hole
rail_hole_pitch      = 20   * MM   # along the rail
rail_hole_end_offset = 10   * MM   # nominal margin from each end


# ── Geometry ──────────────────────────────────────────────────────────────────
class MGN9HRailSlider(BasePart):
    def __init__(self, rail_length: float = 100 * MM, qty: int = 1):
        super().__init__(qty=qty)
        self.rail_length = rail_length

    def name_suffix(self) -> str:
        return f"_{int(self.rail_length)}mm_x{self.qty}"

    def _build(self):
        # Rail: separate body so the slider's groove subtraction can't bite
        # into it. Centered on the X axis, bottom face at z = 0.
        with BuildPart() as rail_p:
            Box(
                self.rail_length, rail_width, rail_height,
                align=(Align.CENTER, Align.CENTER, Align.MIN),
            )
            usable = self.rail_length - 2 * rail_hole_end_offset
            n_holes = max(1, int(usable // rail_hole_pitch) + 1)
            with Locations((0, 0, rail_height)):
                with GridLocations(rail_hole_pitch, 0, n_holes, 1):
                    Hole(radius=rail_hole_dia / 2)

        # Slider sits on the rail; its groove swallows the rail top so the
        # assembly stands 10 mm tall (= rail 6.5 + block 8 - groove 4.5).
        slider_z = rail_height - rail_groove_depth
        with BuildPart() as slider_p:
            with Locations((0, 0, slider_z)):
                Box(
                    block_length, block_width, block_thick,
                    align=(Align.CENTER, Align.CENTER, Align.MIN),
                )
                Box(
                    block_length + 1 * MM,
                    rail_groove_w,
                    rail_groove_depth,
                    align=(Align.CENTER, Align.CENTER, Align.MIN),
                    mode=Mode.SUBTRACT,
                )
                with Locations((0, 0, block_thick)):
                    with GridLocations(mount_pitch_x, mount_pitch_y, 2, 2):
                        Hole(radius=mount_hole_dia / 2, depth=mount_hole_depth)

        return Compound(label="MGN9H assembly",
                        children=[rail_p.part, slider_p.part])


if __name__ == "__main__":
    MGN9HRailSlider().build()
