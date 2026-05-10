"""Headless build of every standard part.

Run from the repo root via:

    /Applications/FreeCAD.app/Contents/MacOS/FreeCAD -c hardware/scripts/build_all_fc.py

Discovers every `parts.<part>.fc` module, imports it, takes its
`PART` instance (a `StandardPart` subclass), calls `PART.build()`,
and writes `<output_name>.FCStd` under `hardware/output/freecad/`
and `<output_name>.step` under `hardware/output/step/fc/`.
"""

import shutil
import sys
from pathlib import Path

# FreeCAD's `-c` invocation doesn't add hardware/ to sys.path, so the
# `parts` package and the sibling `render_views` aren't otherwise findable.
HARDWARE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HARDWARE_DIR))
sys.path.insert(0, str(HARDWARE_DIR / "scripts"))

from parts import discover_part_modules, run_builds  # noqa: E402
from parts._fc import App, Part  # noqa: E402
from render_views import render_part_views  # noqa: E402

OUTPUT_DIR = HARDWARE_DIR / "output"
FCSTD_DIR = OUTPUT_DIR / "freecad"
STEP_DIR = OUTPUT_DIR / "step" / "fc"
VIEWS_DIR = OUTPUT_DIR / "views"

for d in (FCSTD_DIR, STEP_DIR, VIEWS_DIR):
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)


def export_fc(part, result):
    doc, body = result
    doc.saveAs(str(FCSTD_DIR / f"{part.output_name}.FCStd"))
    Part.export([body], str(STEP_DIR / f"{part.output_name}.step"))
    render_part_views(body.Shape, VIEWS_DIR, part.output_name)
    App.closeDocument(doc.Name)


# FreeCAD's `-c <script>` does not always set __name__ == "__main__".
# Run unconditionally so the script works in both modes.
run_builds(discover_part_modules("fc"), export_fc)
