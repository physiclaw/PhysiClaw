"""Base class for assembly-step drawings.

Mirrors ``hardware/parts/base.py``'s ``BasePart`` pattern, but for SVG
drawings instead of STEP exports: subclasses implement ``_build()`` to
return the assembled ``Compound``; calling ``render()`` projects it
through the configured ``camera`` and writes a borderless SVG to
``output_path()``.

Default filename: ``hardware/output/svg/<module_name>.svg`` — derived
from the defining file's basename so ``python -m`` invocation produces
the right name instead of ``__main__.svg``.
"""

import sys
from pathlib import Path

from build123d import MM, Compound, ExportSVG, ShapeList, Unit

from hardware.assembly.render import ISO, Camera, project
from hardware.parts.base import REPO_ROOT

SVG_DIR = REPO_ROOT / "hardware" / "output" / "svg"


class BaseAssembly:
    """Base class for assembly-step drawings.

    Subclasses must override ``_build()`` to return the ``Compound`` to
    draw. Class attributes (``camera``, ``line_weight``, ``page_margin``)
    are overrideable per-step.
    """

    camera: Camera = ISO
    line_weight: float = 0.25   # mm — heavier than build123d's 0.09 default
    page_margin: float = 5 * MM

    @classmethod
    def _module_stem(cls) -> str:
        return Path(sys.modules[cls.__module__].__file__).stem

    def output_path(self) -> Path:
        return SVG_DIR / f"{self._module_stem()}.svg"

    def _build(self) -> Compound:
        raise NotImplementedError

    def build(self) -> Compound:
        """Return the assembled Compound (labeled, no SVG export)."""
        assembly = self._build()
        if not getattr(assembly, "label", None):
            assembly.label = type(self).__name__
        return assembly

    def render(self) -> None:
        assembly = self.build()
        visible, _ = project(assembly, self.camera)
        exporter = ExportSVG(unit=Unit.MM, margin=self.page_margin)
        exporter.add_layer("Visible", line_weight=self.line_weight)
        exporter.add_shape(ShapeList(visible), layer="Visible")

        path = self.output_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        exporter.write(str(path))
        print(f"  wrote {path}")


def render_all(assemblies):
    for a in assemblies:
        a.render()
