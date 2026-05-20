import sys
from pathlib import Path

from build123d import export_step

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
STEP_DIR = REPO_ROOT / "hardware" / "output" / "step"


class BasePart:
    """Base class for parts. Subclasses implement `_build()` to return the
    build123d shape; calling `build()` returns the labeled shape, `export()`
    writes a STEP file to `output_path()`.

    Default filename: `<repo>/hardware/output/step/<module_name>_x<qty>.step`,
    derived from the defining file's basename — so `python -m` invocation
    produces the right name instead of `__main__.step`. Subclasses with extra
    naming dimensions (e.g. length) override `name_suffix()`."""

    def __init__(self, qty: int = 1):
        self.qty = qty

    @classmethod
    def _module_stem(cls) -> str:
        return Path(sys.modules[cls.__module__].__file__).stem

    def name_suffix(self) -> str:
        return f"_x{self.qty}"

    def output_path(self) -> Path:
        return STEP_DIR / f"{self._module_stem()}{self.name_suffix()}.step"

    def _build(self):
        raise NotImplementedError

    def build(self):
        """Return the build123d shape (labeled, no STEP export)."""
        shape = self._build()
        # Default the STEP label to the class name so the exported file is
        # readable in other CAD tools. _build() can override by setting an
        # explicit label (e.g. on a Compound).
        if not getattr(shape, "label", None):
            shape.label = type(self).__name__
        return shape

    def export(self):
        shape = self.build()
        path = self.output_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        export_step(shape, str(path))
        return shape


def export_all(parts):
    for part in parts:
        part.export()
        print(f"  exported {type(part).__name__:20s} → {part.output_path()}")
