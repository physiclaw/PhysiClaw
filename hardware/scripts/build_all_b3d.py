"""Headless build of every standard part using build123d.

Run from the repo root via:

    uv run --group cad python hardware/scripts/build_all_b3d.py

Discovers every `parts.<part>.b3d` module, imports it, takes its
`PART` instance (a `StandardPart` subclass), calls `PART.build()`,
and writes `<output_name>.step` under `hardware/output/step/b3d/`
and `<output_name>.stl` under `hardware/output/stl/`.
"""

import shutil
import sys
from pathlib import Path

# `python script.py` from the repo root doesn't put hardware/ on
# sys.path, so the `parts` package wouldn't otherwise be findable.
HARDWARE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HARDWARE_DIR))

try:
    from build123d import export_step, export_stl  # type: ignore[import-not-found]  # noqa: E402
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "build123d is not installed — sync the `cad` dependency group:\n"
        "    uv sync --group cad"
    ) from exc

from parts import discover_part_modules, run_builds  # noqa: E402

OUTPUT_DIR = HARDWARE_DIR / "output"
STEP_DIR = OUTPUT_DIR / "step" / "b3d"
STL_DIR = OUTPUT_DIR / "stl"

for d in (STEP_DIR, STL_DIR):
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)


def export_b3d(part, geometry):
    export_step(geometry, str(STEP_DIR / f"{part.output_name}.step"))
    export_stl(geometry, str(STL_DIR / f"{part.output_name}.stl"))


if __name__ == "__main__":
    run_builds(discover_part_modules("b3d"), export_b3d)
