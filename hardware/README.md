# PhysiClaw Hardware

This directory holds the **mechanical** side of PhysiClaw — the
physical rig that sits on the desk, controls the robotic stylus, and
sees the phone. The Python under `src/physiclaw/` is the brain; the
files here describe the body.

## What's in here

```text
hardware/
├── README.md                   (this file)
├── lib/
│   ├── parts/                  parametric standard-part generators
│   │   ├── _helpers.py
│   │   ├── motors/      bearings/      pulleys/
│   │   ├── extrusions/  fasteners/
│   ├── scripts/
│   │   ├── build_all.py        builds every part in PARTS
│   │   └── render_views.py     wireframe SVG renderer
├── custom/                     hand-designed parts (FreeCAD GUI) — empty scaffold
├── assembly/                   assemblies of standard + custom parts — empty scaffold
└── freecad/  step/  stl/  views/    build outputs — gitignored
```

The split matters: **standard parts** (off-the-shelf bolts, motors,
bearings) live as Python under `lib/parts/`. **Custom parts**
(PhysiClaw-specific brackets, mounts, stylus holders) belong as
hand-edited `.FCStd` files in `custom/`. **Assemblies** combine the
two into rig configurations.

`custom/` and `assembly/` are empty for now — placeholders for the
GUI-authored work. Standard parts aren't a complete BOM either; they
are the recurring shapes worth code-generating.

## Design philosophy

Standard parts are **code-first, parametric, regenerated**:

- Each part is a `build()` function that constructs a FreeCAD
  document programmatically using Part Design features (Pad, Pocket,
  Fillet, PolarPattern) and a single `Parameters` VarSet that holds
  every dimension.
- Edit a parameter (say, M3 length 10 → 20 mm) in the GUI, press
  F5, geometry recomputes. Or edit the Python and re-run the build.
- The build is idempotent — `build_all.py` regenerates `freecad/`,
  `step/`, and `views/` from scratch every run.
- Topology never silently drifts: a topology change requires a new
  versioned filename (`_v2`) so future assemblies can pin a stable
  reference.

Custom parts go the other way: drawn in the FreeCAD GUI, committed
as `.FCStd` binaries. Code-generation isn't worth the friction for
one-offs.

## Standard parts in the library

| Part | What it is |
| ------ | ------------ |
| **NEMA 17 stepper** | 42×42 mm stepper motor, 5 mm shaft, 31 mm hole spacing. |
| **GT2 20T pulley** | 20-tooth GT2 timing-belt pulley, 5 mm bore. |
| **608ZZ bearing** | 8 × 22 × 7 mm radial ball bearing. |
| **2020 extrusion** | 20×20 mm aluminum T-slot, parametric length. |
| **M3 cap screw** | ISO 4762 socket-head, parametric length. |

Each is a simplified envelope — accurate enough for clearance,
mounting, and visualization, not for drop-in CNC machining.

## Building

```bash
/Applications/FreeCAD.app/Contents/MacOS/FreeCAD -c hardware/lib/scripts/build_all.py
```

Outputs land in `freecad/` (editable), `step/` (interchange), and
`views/` (multi-angle wireframe SVGs — quick visual sanity check
that the geometry is what you expected).

## Adding a part

1. Pick a family directory under `lib/parts/` or create one
   (`__init__.py` required).
2. Copy the most similar existing generator as a starting point —
   `bearing_608.py` for cylindrical, `nema17.py` for blocks with
   features.
3. Register the new module in `build_all.py`'s `PARTS` list.
4. Run the build, scan the SVGs in `views/` to confirm shape and
   bounding-box dimensions.

The helpers in `lib/parts/_helpers.py` cover the recurring patterns
(VarSet creation, sketch attachment, pad/pocket binding, polar
patterns, hole arrays). Read the existing parts to see them in use.

## Editing existing parts

Two paths:

- **Quick tweak:** open `freecad/<part>.FCStd` in the FreeCAD GUI,
  edit a `Parameters` property, F5, re-export. The next
  `build_all.py` run will overwrite the change.
- **Permanent change:** edit the Python in
  `lib/parts/<family>/<part>.py` and re-run the build.

For variant runs (e.g. M3 in 6/8/10/12/16/20 mm lengths), give the
generator's `build()` a `params` kwarg and loop in `build_all.py`.

## Conventions

- VarSet is always named `Parameters`; properties in PascalCase
  (`BodySize`, `HoleSpacing`).
- Use `Length` for any dimension (preserves mm units in expressions
  — don't use `Float`); `Integer` for counts; `Angle` for angles.
- Object labels: `Sketch_<Purpose>`, `Pad_<Purpose>`,
  `Pocket_<Purpose>`, `Fillet_<Purpose>`, `Pattern_<Purpose>`.
- Filenames: `lowercase_underscore.py` for source. Output names use
  the part's standard designator (`M3x10`, `NEMA17`, `Bearing_608`,
  `GT2_20T`, `Extrusion_2020_L300`) — readable, not an enforced
  case.
- Don't bake variants into filenames — VarSet handles dimensions.
- VarSet expressions use `<<Parameters>>.PropName`, not the legacy
  `Spreadsheet.alias` form.

## References

- FreeCAD Parts Library:
  <https://github.com/FreeCAD/FreeCAD-library>
- ISO 4762 (M3 cap screw), NEMA ICS 16-2001 (stepper mounts).
