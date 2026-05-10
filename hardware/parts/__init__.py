"""Standard-parts catalog with the abstract base it plugs into.

Each subdirectory under `parts/` is one part: a `spec.py` (frozen
dataclass of dimensions, no backend imports) plus one or more
backend modules (`fc.py`, `b3d.py`) that subclass `StandardPart`,
implement `build()`, and export a singleton `PART = MyPart(MY_SPEC)`.

The driver scripts (`hardware/scripts/build_all_fc.py`,
`build_all_b3d.py`) call `discover_part_modules(backend)` to
enumerate the matching backend modules, import each, and call
`PART.build()`.

`build()` returns whatever the backend driver expects:

- FreeCAD: `(doc, body)`
- build123d: a `build123d.Part`
"""

import importlib
import sys
import time
import traceback
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Literal

Backend = Literal["fc", "b3d"]


class Spec:
    """Marker base class for part dimension specs.

    Subclasses must be `@dataclass(frozen=True)` with primitive
    (numeric / string) fields only. A spec describes geometry — it
    doesn't compute it; no backend imports, no methods.
    """


def discover_part_modules(backend: Backend) -> list[str]:
    """Return sorted dotted names of `parts.<part>.<backend>` modules
    that exist on disk (e.g. `discover_part_modules("fc")` →
    `["parts.bearing_608.fc", "parts.m3_screw.fc", ...]`).

    Walks the filesystem rather than importing, so a part dir without
    the requested backend is silently skipped (e.g. a part with
    `fc.py` but no `b3d.py` won't show up in the b3d build). Part
    directories whose name starts with `_` are skipped so private
    helpers (`_fc.py`, `_helpers.py`) don't get pulled in.
    """
    pkg_dir = Path(__path__[0])
    names = []
    for entry in sorted(pkg_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith("_"):
            continue
        if (entry / f"{backend}.py").is_file():
            names.append(f"{__package__}.{entry.name}.{backend}")
    return names


class StandardPart(ABC):
    """Base class for parametric standard parts.

    Subclasses must set `output_name` (the export filename stem, used
    for `<name>.FCStd` / `<name>.step` / `<name>.stl`) and implement
    `build()`. A `Spec` instance carries the dimensional data and is
    accessible as `self.spec` inside `build()`.
    """

    output_name: str

    def __init__(self, spec: Spec) -> None:
        self.spec = spec

    @abstractmethod
    def build(self) -> Any:
        ...


def run_builds(
    part_modules: list[str],
    export: Callable[["StandardPart", Any], None],
) -> None:
    """Drive the build loop common to every backend: import each part
    module, call `PART.build()`, hand the result to `export`, log
    OK/ERR, and exit non-zero if any part failed.
    """
    t0 = time.time()
    success, failed = 0, []
    for module_name in part_modules:
        try:
            mod = importlib.import_module(module_name)
            part = mod.PART
            export(part, part.build())
            print(f"  OK  {part.output_name}")
            success += 1
        except Exception as e:
            print(f"  ERR {module_name}: {e}")
            traceback.print_exc()
            failed.append(module_name)

    print(f"\nBuilt {success}/{len(part_modules)} in {time.time() - t0:.1f}s")
    if failed:
        print(f"Failed: {', '.join(failed)}")
        sys.exit(1)


__all__ = ["Backend", "Spec", "StandardPart", "discover_part_modules", "run_builds"]
