import sys
from pathlib import Path
from typing import Literal

from build123d import export_step

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
STEP_DIR = REPO_ROOT / "hardware" / "output" / "step"

BomCategory = Literal["standard", "custom"]

# Append-only registry; collect() clears + reaggregates. A list rather
# than a dict keyed by id(shape) because shape transforms (.move /
# .rotate) return new objects — id-lookups would silently drop any
# transformed part.
BOM_REGISTRY: list[tuple[tuple, int, BomCategory, str | None]] = []


class BasePart:
    """Base class for parts. Subclasses implement `_build()` to return the
    build123d shape; calling `build()` returns the labeled shape, `export()`
    writes a STEP file to `output_path()`.

    Default filename: `<repo>/hardware/output/step/<module_name>_x<qty>.step`,
    derived from the defining file's basename — so `python -m` invocation
    produces the right name instead of `__main__.step`. Subclasses with extra
    naming dimensions (e.g. length) override `name_suffix()`.

    BOM identity: ``bom_key()`` returns a hashable spec tuple (default the
    class name); ``bom_category`` is "standard" (purchasable) or "custom"
    (manufactured). Override ``bom_key`` on subclasses with parameters
    (Screw, TNut, …) and use ``BaseCustomPart`` for manufactured parts
    instead of setting ``bom_category`` by hand. Assemblies opt out by
    returning None from ``bom_key``."""

    bom_category: BomCategory = "standard"

    def __init__(self, qty: int = 1):
        self.qty = qty

    @classmethod
    def _module_stem(cls) -> str:
        return Path(sys.modules[cls.__module__].__file__).stem

    def name_suffix(self) -> str:
        return f"_x{self.qty}"

    def output_path(self) -> Path:
        return STEP_DIR / f"{self._module_stem()}{self.name_suffix()}.step"

    def bom_key(self):
        """Hashable identity used to aggregate this part in the BOM.

        Default uses the class name alone — fine for zero-arg parts
        (Bumper, Tip). Override to include parameters that distinguish
        instances (size, length, kind). Return None to exclude this part
        from the BOM (assemblies do this)."""
        return (type(self).__name__,)

    def bom_display(self) -> str | None:
        """Optional human-readable label for the BOM table. Return None
        to use the default algorithm in ``hardware.bom.bom.BomEntry``
        (head + space-joined strings + ×-prefixed numbers). Override
        when that default doesn't read naturally for a specific part."""
        return None

    def _build(self):
        raise NotImplementedError

    def build(self):
        """Return the build123d shape (labeled, no STEP export).

        Memoized on the instance: callers like ``export()`` + ``render()``
        on the same object share one build instead of running ``_build()``
        twice. _build() is expected to be a pure function of self.
        """
        if (cached := getattr(self, "_built", None)) is not None:
            return cached
        shape = self._build()
        # Default the STEP label to the class name so the exported file is
        # readable in other CAD tools. _build() can override by setting an
        # explicit label (e.g. on a Compound).
        if not getattr(shape, "label", None):
            shape.label = type(self).__name__
        self._built = shape
        key = self.bom_key()
        if key is not None:
            BOM_REGISTRY.append((key, self.qty, self.bom_category, self.bom_display()))
        return shape

    def export(self):
        shape = self.build()
        path = self.output_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        export_step(shape, str(path))
        return shape


class BaseCustomPart(BasePart):
    """Parts manufactured for this build (printed / machined). Identical
    to BasePart except BOM aggregation tags them as ``custom`` so the
    writer renders them in their own section."""

    bom_category: BomCategory = "custom"


def export_all(parts):
    for part in parts:
        part.export()
        print(f"  exported {type(part).__name__:20s} → {part.output_path()}")
