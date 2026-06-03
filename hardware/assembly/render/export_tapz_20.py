"""Build the tapz_20 assembly, color-tag every leaf, write the .glb
the Blender stage consumes.

build123d 0.10.0's glTF writer keeps `.color` but loses `.label`, so
materials are smuggled across via HSV-encoded sRGB tags on each
labelled compound. XCAF then propagates that color down to any
unlabelled sub-shapes (e.g. the raw Part children inside MGN9H).
"""

from __future__ import annotations

import os
import sys
from collections import Counter

from build123d import Color, Compound, Unit, export_gltf

# Make sibling materials_table importable when invoked as a script.
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from materials_table import MATERIAL_LIST, encode_color  # noqa: E402

# Add project root so `hardware.…` resolves regardless of CWD.
from pathlib import Path
PROJECT_ROOT = str(Path(HERE).resolve().parent.parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from hardware.parts.base import REPO_ROOT  # noqa: E402
from hardware.assembly.procedures.tapz_20_solenoid_mount import (  # noqa: E402
    TZ20SolenoidMount,
)

RENDER_DIR = REPO_ROOT / "hardware" / "output" / "render"

# Material assignment: exact-label match first, then label-prefix.
EXACT_LABEL_TO_MATERIAL = {
    "Pulley2GT20T":     "Aluminum_Polished",
    "pulley":           "Aluminum_Polished",
    "Ring":             "Aluminum_Polished",
    # One label covers all three pieces of the solenoid (shell + plunger
    # rod above + rod below) — it's a single Compound in the CAD model.
    "MGN9H":            "Steel_Chrome",
    "Solenoid":         "Steel_Chrome",
    "Belt":             "Rubber_Belt",
    "Bumper":           "Rubber_Bumper",
    # NEMA17 housing painted to match the anodized frame — same look
    # and band of darkness as the extrusions, no separate housing tone.
    "Nema17Motor":      "Aluminum_Anod_Black",
    "Nut":              "Steel_Zinc",
    "FlatBracket":      "PA12_Black_MJF",
    "MotorBracket":     "PA12_Black_MJF",
    "IdlerMountMotor": "PA12_Black_MJF",
    "IdlerMountFront": "PA12_Black_MJF",
    "BeltClamp":        "PA12_Black_MJF",
    "SolenoidMount":    "PA12_Black_MJF",
    "Tip":              "PA12_Black_MJF",
    "XyJointLeft":      "PA12_Black_MJF",
    "XyJointRight":     "PA12_Black_MJF",
}
PREFIX_TO_MATERIAL = {
    "Extrusion": "Aluminum_Anod_Black",
    "Screw_":    "Steel_Black_Coated",
    "TNut_":     "Steel_Zinc",
}

# Fallback for leaves whose nearest labelled ancestor matched nothing —
# in practice the un-labelled raw Solids that BuildPart returns inside
# the MGN9H compound.
DEFAULT_MATERIAL = "Steel_Chrome"


def _material_for(label: str) -> str | None:
    if not label:
        return None
    if label in EXACT_LABEL_TO_MATERIAL:
        return EXACT_LABEL_TO_MATERIAL[label]
    for prefix, mat in PREFIX_TO_MATERIAL.items():
        if label.startswith(prefix):
            return mat
    return None


def _tag(node, inherited: str | None, stats: Counter) -> int:
    """Paint labelled nodes; XCAF inherits to unlabelled sub-shapes.

    Returns the leaf count under this node so the histogram in stats[]
    is built in one pass instead of a second tree walk per ancestor.
    """
    label = getattr(node, "label", "") or ""
    effective = _material_for(label) or inherited
    children = list(getattr(node, "children", []) or [])

    # Only labelled nodes are findable in the exported XCAF document.
    if label and effective is not None:
        node.color = Color(*encode_color(effective))

    if not children:
        stats[effective or DEFAULT_MATERIAL] += 1
        return 1
    return sum(_tag(c, effective, stats) for c in children)


def build_and_tag(exploded: bool = False) -> Compound:
    asm = TZ20SolenoidMount(exploded=exploded).build()
    stats: Counter = Counter()
    leaves = _tag(asm, inherited=None, stats=stats)
    print(f"Tagged {leaves} leaf parts across {len(stats)} materials:")
    for mat, n in stats.most_common():
        print(f"  {n:4d}  {mat}")
    unused = [name for name, _ in MATERIAL_LIST if name not in stats]
    if unused:
        print(f"  (unused: {', '.join(unused)})")
    return asm


def main() -> Path:
    RENDER_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RENDER_DIR / f"{TZ20SolenoidMount._module_stem()}.glb"
    asm = build_and_tag(exploded=False)
    export_gltf(
        asm,
        str(out_path),
        unit=Unit.MM,
        binary=True,
        linear_deflection=0.01,
        angular_deflection=0.1,
    )
    print(f"wrote {out_path} ({out_path.stat().st_size / (1024 * 1024):.1f} MB)")
    return out_path


if __name__ == "__main__":
    main()
