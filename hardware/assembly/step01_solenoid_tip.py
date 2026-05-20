"""Assembly step 01 — solenoid + tip.

Single-step drawing showing the stylus tip pressed onto the solenoid's
bottom rod. The rod's lower 4 mm is grooved (thread-like) and grips the
tip's M3 hole bore until the rod tip bottoms in the hole.

Output: ``hardware/output/svg/step01_solenoid_tip.svg`` — borderless
sheet sized to its own bbox, single trimetric view.

Run from the repo root:

    uv run --group cad python -m hardware.assembly.step01_solenoid_tip
"""

from build123d import MM, Compound, ExportSVG, ShapeList, Unit

from hardware.assembly.camera import Camera
from hardware.parts.base import REPO_ROOT
from hardware.parts.standard.solenoid import Solenoid
from hardware.parts.standard.tip import Tip

SVG_DIR = REPO_ROOT / "hardware" / "output" / "svg"
OUT_PATH = SVG_DIR / "step01_solenoid_tip.svg"

PAGE_MARGIN = 5 * MM
# Camera placed at this multiple of the part's reach from world origin.
# 4× leaves noticeable perspective foreshortening — bump higher to push
# the projection toward orthographic.
CAMERA_DISTANCE_FACTOR = 4


def build_assembled() -> Compound:
    """Build solenoid + tip with the tip seated on the bottom rod."""
    solenoid = Solenoid()._build()
    tip = Tip()._build()
    solenoid.joints["tip_mount"].connect_to(tip.joints["solenoid_mount"])
    return Compound(label="solenoid_tip_assembled", children=[solenoid, tip])


def main():
    SVG_DIR.mkdir(parents=True, exist_ok=True)

    assembled = build_assembled()
    bbox = assembled.bounding_box()
    reach = max(abs(v) for v in (
        bbox.min.X, bbox.max.X, bbox.min.Y, bbox.max.Y, bbox.min.Z, bbox.max.Z,
    ))
    cam = Camera(-60, -10, roll=60)
    camera_pos = bbox.center() + cam * (reach * CAMERA_DISTANCE_FACTOR)
    visible, _ = assembled.project_to_viewport(camera_pos, cam.up)

    exporter = ExportSVG(unit=Unit.MM, margin=PAGE_MARGIN)
    exporter.add_layer("Visible")
    exporter.add_shape(ShapeList(visible), layer="Visible")
    exporter.write(str(OUT_PATH))
    print(f"  wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
