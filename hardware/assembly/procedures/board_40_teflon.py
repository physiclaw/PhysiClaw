"""Teflon tube on the board sub-assembly — routes a PTFE tube from the PCB
holder's left-face bore (the "upper tube" M5 hole) to the solenoid mount's
keyboard-face slot, draped over the board_30 assembly.

Both port locations are read out of the built assembly so the routing stays
correct if upstream placement changes:
  * End A — the holder's left cylinder boss bore mouth, carried into world by
    the holder's BOARD_PLACEMENT transform.
  * End B — the solenoid mount's keyboard slot, carried by the SolenoidMount's
    global location within the assembly.
The tube routes as an inverted-U (∩): it leaves the holder bore heading +Y (up,
the frame's up axis), arcs over an apex, and arrives at the slot heading -Y.
Those opposing end tangents make Teflon thread the spine through an auto-computed
apex (a single spline would self-intersect), keeping the route clear above.

Two variants:
  * exploded — tube lifted off (+X) to show it separated from the ports.
  * assembled — tube seated between the two ports.

Run from the repo root:

    uv run --group cad python -m hardware.assembly.procedures.board_40_teflon
"""

from build123d import Compound, Location, Vector

from hardware.assembly.base import BaseAssembly
from hardware.assembly.procedures.board_20_frame import BOARD_PLACEMENT
from hardware.assembly.procedures.board_30_pcb import BO30Pcb
from hardware.assembly.projection import MAIN_FRAME_VIEW
from hardware.parts.custom import pcb_holder as H
from hardware.parts.custom import solenoid_mount as S
from hardware.parts.standard.teflon import Teflon

TUBE_OD        = 4      # mm — PTFE OD (fits the holder's 5 mm bore)
TUBE_ID        = 2      # mm
TANGENT_SCALAR = 2.0    # Bézier-handle length (tall legs; sweeps the tall apex)
EXPLODE        = 40     # mm — exploded: tube lifted off along +Y

# Inverted-U (∩): frame up is +Y, so the tube leaves the holder bore heading +Y
# (up toward the apex) and arrives at the slot heading -Y (down from the apex).
# These oppose, so Teflon threads the spine through an auto-computed apex.
DIR_UP   = (0, 1, 0)
DIR_DOWN = (0, -1, 0)

def _global_location(node, label, acc=None):
    """Composed world Location of the first leaf labeled ``label``."""
    acc = (acc or Location()) * node.location
    if getattr(node, "label", None) == label and not node.children:
        return acc
    for child in node.children:
        found = _global_location(child, label, acc)
        if found is not None:
            return found
    return None


def _world_point(loc: Location, p) -> Vector:
    """World position of native point ``p`` under Location ``loc``."""
    return (loc * Location(Vector(*p))).position


class BO40Teflon(BaseAssembly):
    camera = MAIN_FRAME_VIEW

    def _build(self) -> Compound:
        base = BO30Pcb(exploded=False).build()

        # End A: holder left-cylinder bore mouth (native) → world.
        bore = (-H.plate_half_x + H.left_cyl_embed - H.left_cyl_len, 0, H.thickness / 2)
        p1 = _world_point(BOARD_PLACEMENT, bore)

        # End B: solenoid keyboard-face slot (native) → world.
        sol = _global_location(base, "SolenoidMount")
        join_y = S.width / 2 - S.wall_thickness
        slot = (0, join_y - S.keyboard_slot_h / 2, S.thickness / 2)
        p2 = _world_point(sol, slot)

        # Inverted-U: bore end +Y, slot end -Y so the auto-apex (bulging along
        # d1-d2) sits ABOVE both ports (+Y). Teflon adds that apex.
        tube = Teflon(
            point1=tuple(p1), direction1=DIR_DOWN,
            point2=tuple(p2), direction2=DIR_UP,
            od=TUBE_OD, id=TUBE_ID,
            tangent_scalars=(TANGENT_SCALAR, TANGENT_SCALAR),
        ).build()
        if self.exploded:
            tube.move(Location((0, EXPLODE, 0)))

        return Compound(label="board_40_teflon", children=[base, tube])


if __name__ == "__main__":
    for exploded in (True, False):
        asm = BO40Teflon(exploded=exploded)
        asm.export()
        asm.render()
