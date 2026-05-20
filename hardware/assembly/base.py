"""Base class for assembly-step drawings.

``BaseAssembly`` inherits from ``BasePart`` — assemblies share the
build/export/output_path machinery (so ``.export()`` writes a STEP of
the composed assembly, handy for CAD inspection) and add ``.render()``
for the SVG drawing used in the manual.

Default filenames:
  * ``hardware/output/step/<module_name>.step`` (inherited)
  * ``hardware/output/svg/<module_name>.svg``  (this class)
"""

from pathlib import Path

from build123d import MM, Compound, ExportSVG, ShapeList, Unit

from hardware.assembly.render import ISO, Camera, project
from hardware.parts.base import REPO_ROOT, BasePart

SVG_DIR = REPO_ROOT / "hardware" / "output" / "svg"


class BaseAssembly(BasePart):
    """Buildable assembly — STEP via .export() (inherited), SVG via .render()."""

    camera: Camera = ISO
    line_weight: float = 0.25   # mm — heavier than build123d's 0.09 default
    page_margin: float = 5 * MM

    def name_suffix(self) -> str:
        # Assemblies are one-offs; drop the inherited "_x{qty}" suffix so the
        # STEP filename is e.g. solenoid_tip.step instead of solenoid_tip_x1.step.
        return ""

    def svg_path(self) -> Path:
        return SVG_DIR / f"{self._module_stem()}.svg"

    def _build(self) -> Compound:
        raise NotImplementedError

    def render(self) -> None:
        assembly = self.build()
        visible, _ = project(assembly, self.camera)
        exporter = ExportSVG(unit=Unit.MM, margin=self.page_margin)
        exporter.add_layer("Visible", line_weight=self.line_weight)
        exporter.add_shape(ShapeList(visible), layer="Visible")

        path = self.svg_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        exporter.write(str(path))
        print(f"  wrote {path}")


def render_all(assemblies):
    for a in assemblies:
        a.render()
