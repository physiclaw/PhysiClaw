"""Headless build of every standard part.

Run from the repo root via:

    /Applications/FreeCAD.app/Contents/MacOS/FreeCAD -c hardware/lib/scripts/build_all.py

Reads `PARTS`, calls each module's `build()`, writes `<out>.FCStd` under
`hardware/freecad/` and `<out>.step` under `hardware/step/`.
"""

import importlib
import sys
import time
import traceback
from pathlib import Path

import FreeCAD as App
import Part

LIB_DIR = Path(__file__).resolve().parent.parent           # hardware/lib
HARDWARE_DIR = LIB_DIR.parent                              # hardware
FCSTD_DIR = HARDWARE_DIR / "freecad"
STEP_DIR = HARDWARE_DIR / "step"
VIEWS_DIR = HARDWARE_DIR / "views"

for d in (FCSTD_DIR, STEP_DIR, VIEWS_DIR):
    d.mkdir(parents=True, exist_ok=True)

# Make `parts` package importable when FreeCAD runs the script with -c
sys.path.insert(0, str(LIB_DIR))
sys.path.insert(0, str(LIB_DIR / "scripts"))

from render_views import render_part_views  # noqa: E402

# (module path, output filename)
PARTS = [
    ("parts.fasteners.m3_screw",        "M3x10"),
    ("parts.bearings.bearing_608",      "Bearing_608"),
    ("parts.extrusions.extrusion_2020", "Extrusion_2020_L300"),
    ("parts.motors.nema17",             "NEMA17"),
    ("parts.pulleys.gt2_20t",           "GT2_20T"),
]


def main():
    t0 = time.time()
    success, failed = 0, []
    for module_name, out_name in PARTS:
        try:
            mod = importlib.import_module(module_name)
            doc, body = mod.build()

            doc.saveAs(str(FCSTD_DIR / f"{out_name}.FCStd"))
            Part.export([body], str(STEP_DIR / f"{out_name}.step"))
            render_part_views(body.Shape, VIEWS_DIR, out_name)

            App.closeDocument(doc.Name)
            print(f"  OK  {out_name}")
            success += 1
        except Exception as e:
            print(f"  ERR {out_name}: {e}")
            traceback.print_exc()
            failed.append(out_name)

    print(f"\nBuilt {success}/{len(PARTS)} in {time.time() - t0:.1f}s")
    if failed:
        print(f"Failed: {', '.join(failed)}")
        sys.exit(1)


# FreeCAD's `-c <script>` does not always set __name__ == "__main__".
# Call main() unconditionally so the script runs in both modes.
main()
