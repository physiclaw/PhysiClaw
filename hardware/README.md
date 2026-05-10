# PhysiClaw Hardware

This directory holds the **mechanical** side of PhysiClaw — the
physical rig that sits on the desk, controls the robotic stylus, and
sees the phone. The Python under `src/physiclaw/` is the brain; the
files here describe the body.

## What's in here

```text
hardware/
├── README.md                   (this file)
├── parts/                      one directory per part — spec + per-backend generators
│   ├── __init__.py                  Spec, StandardPart, discover_part_modules, run_builds
│   ├── _fc.py    _helpers.py        FreeCAD imports + sketch/pad helpers (shared)
│   ├── bearing_608/    {spec.py, fc.py, b3d.py}
│   ├── m3_screw/       {spec.py, fc.py}
│   ├── nema17/         {spec.py, fc.py}
│   ├── extrusion_2020/ {spec.py, fc.py}
│   └── gt2_20t/        {spec.py, fc.py}
├── scripts/
│   ├── build_all_fc.py         headless FreeCAD build → output/{freecad,step/fc,views}/
│   ├── build_all_b3d.py        headless build123d build → output/{step/b3d,stl}/
│   └── render_views.py         wireframe SVG renderer (FreeCAD only)
├── custom/                     hand-designed parts (FreeCAD GUI) — empty scaffold
├── assembly/                   assemblies of standard + custom parts — empty scaffold
└── output/                     build outputs (freecad/ step/ stl/ views/) — gitignored
```

The split matters: **standard parts** (off-the-shelf bolts, motors,
bearings) live as Python under `parts/<part>/` — one directory
per part, each containing `spec.py` (dimensions, plain Python),
`fc.py` (FreeCAD generator), and optionally `b3d.py` (build123d
generator). **Custom parts** (PhysiClaw-specific brackets, mounts,
stylus holders) belong as hand-edited `.FCStd` files in `custom/`.
**Assemblies** combine the two into rig configurations.

`custom/` and `assembly/` are empty for now — placeholders for
the GUI-authored work. The standard-parts library isn't a
complete BOM either: it covers the recurring shapes worth
code-generating, not every fastener and bracket the rig needs.

## Design philosophy

Standard parts are **code-first, parametric, regenerated**:

- Each part is a `StandardPart` subclass whose `build()` method
  constructs the geometry programmatically. Dimensions come from a
  frozen `Spec` dataclass in `parts/<part>/spec.py`, passed into
  the part's `__init__` and read as `self.spec` inside `build()` —
  so the FreeCAD and build123d definitions can't drift.
- Edit a value in `parts/<part>/spec.py` (say, M3 length 10 → 20
  mm) and re-run the matching build script — geometry regenerates
  from scratch.
- The build is idempotent — each driver wipes the directories it
  owns and regenerates from scratch. `build_all_fc.py` rewrites
  `output/freecad/`, `output/step/fc/`, and `output/views/`;
  `build_all_b3d.py` rewrites `output/step/b3d/` and
  `output/stl/`. The two backends own separate `step/` subdirs so
  re-running one doesn't clobber the other's outputs.
- Topology changes are breaking changes for downstream assemblies.
  Once an assembly pins to a part's `output_name`, a topology
  change requires a new versioned name (e.g. `Bearing_608_v2`);
  until then, in-place rewrites are fine.

Custom parts go the other way: drawn in the FreeCAD GUI, committed
as `.FCStd` binaries. Code-generation isn't worth the friction for
one-offs.

## Two backends

A part directory can hold a FreeCAD generator (`fc.py`), a
build123d generator (`b3d.py`), or both. Each generator reads
dimensions from the part's `spec.py`. Auto-discovery picks up
whichever backend modules exist — a part dir without `b3d.py` is
simply absent from the build123d build set.

|                    | FreeCAD path                              | build123d path             |
| ------------------ | ----------------------------------------- | -------------------------- |
| Runtime            | FreeCAD's embedded Python                 | plain `uv run`             |
| Driver             | `build_all_fc.py` (via `FreeCAD -c …`)    | `build_all_b3d.py`         |
| Outputs            | `.FCStd` + `.step` + `.svg` views         | `.step` + `.stl`           |
| Editing            | code, or GUI VarSet then F5               | code only                  |
| Testable in CI     | no (needs FreeCAD)                        | yes                        |

### Why two backends — cross-checking

The two backends are not redundant; they are a cross-check. Both
read from the same `spec.py` but use very different APIs
(FreeCAD's PartDesign feature tree vs build123d's CSG-style
boolean ops). When both produce the same shape, that shape is
highly likely correct. When they diverge, one of the generators
has a bug, and the divergence usually points at it.

**Whenever you change `fc.py` or `b3d.py` for a part that has
both backends, build both and cross-check the resulting STEP
files.** FreeCAD's and build123d's STEP exporters produce wildly
different file sizes for the same geometry, so file size on disk
is not a valid check — compare topology and metrics:

```bash
/Applications/FreeCAD.app/Contents/MacOS/FreeCAD -c hardware/scripts/build_all_fc.py
uv run --group cad python hardware/scripts/build_all_b3d.py
uv run --group cad python -c "
from build123d import import_step
PART = 'Extrusion_2020_L300'  # change to the part you're checking
fc  = import_step(f'hardware/output/step/fc/{PART}.step')
b3d = import_step(f'hardware/output/step/b3d/{PART}.step')
print(f'volume:      FC={fc.volume:.1f}  B3D={b3d.volume:.1f}  Δ={fc.volume - b3d.volume:.3f}')
print(f'bounding:    FC={fc.bounding_box()}')
print(f'             B3D={b3d.bounding_box()}')
print(f'face count:  FC={len(fc.faces())}  B3D={len(b3d.faces())}')
"
```

A passing cross-check shows equal volumes (down to a fraction of
a mm³), identical bounding boxes, and matching face counts.
Failure modes:

- **Mismatched face count** → one backend has extra or missing
  features (a fillet that didn't apply, a pocket that didn't
  fuse, a missing rib).
- **Matching face count but different volume** → a dimension is
  off — usually a typo in one backend or a stale literal that
  should have come from `spec.py`.

Worked example: when the 2020 extrusion was first ported to
build123d, `fc.py` was building the simplified "solid block with
T-slot pockets" shape while `b3d.py` was building the faithful
"centre block + 4 diagonal ribs" topology. Both volume and face
count diverged sharply — `fc.py` was over-volume because it
hadn't carved out the inter-rib voids. The fix was rewriting
`fc.py` to match b3d's topology, with rib pads extended slightly
at each end to overlap the centre block and corner pads (bare
endpoint contact won't fuse in PartDesign).

## Standard parts in the library

| Part | What it is |
| ------ | ------------ |
| **NEMA 17 stepper** | 42×42 mm stepper motor, 5 mm shaft, 31 mm hole spacing. |
| **GT2 20T pulley** | 20-tooth GT2 timing-belt pulley, 5 mm bore. |
| **608ZZ bearing** | 8 × 22 × 7 mm radial ball bearing. |
| **2020 extrusion** | 20×20 mm aluminum T-slot, parametric length. |
| **M3 cap screw** | ISO 4762 socket-head, parametric length. |

Each is a simplified model — accurate enough for clearance,
mounting, and visualization, not for drop-in CNC machining or
simulation. Most parts drop fine details (thread, ball-bearing
internals, NEMA shaft flats); the 2020 extrusion keeps full
cross-section topology but omits the 0.5 mm retention lip and
the cavity-back chamfers in the published spec.

## Building

```bash
# FreeCAD path — editable .FCStd + .step + SVG views
/Applications/FreeCAD.app/Contents/MacOS/FreeCAD -c hardware/scripts/build_all_fc.py

# build123d path — .step + .stl
uv run --group cad python hardware/scripts/build_all_b3d.py
```

Outputs land under `output/`: `output/freecad/` (editable, FreeCAD
only), `output/step/{fc,b3d}/` (per-backend STEP — diff against
each other to cross-check), `output/stl/` (build123d only), and
`output/views/` (multi-angle wireframe SVGs — FreeCAD only, quick
visual sanity check that the geometry is what you expected).

## Adding a part

1. Create the part directory `parts/<part>/` with an empty
   `__init__.py`.
2. Add `spec.py`: a `@dataclass(frozen=True)` inheriting from
   `parts.Spec` plus a module-level instance (no geometry, no
   backend imports). Copy `parts/bearing_608/spec.py` as a template.
3. Add `fc.py` (and/or `b3d.py`). Copy the most similar existing
   generator as a starting point — for FreeCAD: `bearing_608/fc.py`
   for cylindrical, `nema17/fc.py` for blocks with features. The
   generator subclasses `StandardPart`, sets `output_name`,
   implements `build()`, and exports a singleton
   `PART = MyPart(MY_SPEC)` at module scope.
4. Run the build. `build_all_fc.py` walks `parts/` and picks up
   every directory containing an `fc.py`; `build_all_b3d.py`
   does the same for `b3d.py`. There is no list to register
   against. Scan `output/views/` (FreeCAD) or the STL viewer of
   your choice (build123d) to confirm shape and bounding-box
   dimensions, and run the cross-check above if the part has
   both backends.

The helpers in `parts/_helpers.py` cover the recurring FreeCAD
patterns (VarSet creation, sketch attachment, pad/pocket binding,
polar patterns, hole arrays). Read the existing `fc.py` modules to
see them in use. A `_b3d_helpers.py` may join it as the build123d
catalogue widens.

## Editing existing parts

Two paths:

- **Quick tweak (FreeCAD only):** open `output/freecad/<part>.FCStd` in the
  FreeCAD GUI, edit a `Parameters` property, F5, re-export. The next
  `build_all_fc.py` run will overwrite the change.
- **Permanent change:** edit `parts/<part>/spec.py` (dimension
  change) or `parts/<part>/<backend>.py` (geometry change) and
  re-run the matching build script.

For variant runs (e.g. M3 in 6/8/10/12/16/20 mm lengths), define a
spec instance per variant in `parts/<part>/spec.py` and one
part directory per variant whose `<backend>.py` exports
`PART = MyPart(VARIANT_SPEC)` with a distinct `output_name`.
Auto-discovery picks them all up.

## Conventions

- Specs are `@dataclass(frozen=True)` instances; field names carry
  units (`inner_diameter_mm`, `width_mm`).
- FreeCAD VarSet is always named `Parameters`; properties in
  PascalCase (`BodySize`, `HoleSpacing`).
- Use `Length` for any FreeCAD dimension (preserves mm units in
  expressions — don't use `Float`); `Integer` for counts; `Angle` for
  angles.
- Object labels: `Sketch_<Purpose>`, `Pad_<Purpose>`,
  `Pocket_<Purpose>`, `Fillet_<Purpose>`, `Pattern_<Purpose>`.
- Filenames: `lowercase_underscore.py` for source. Output names use
  the part's standard designator (`M3x10`, `NEMA17`, `Bearing_608`,
  `GT2_20T`, `Extrusion_2020_L300`) — readable, not an enforced
  case.
- Don't bake variants into filenames — the `Spec` dataclass and VarSet handle dimensions.
- VarSet expressions use `<<Parameters>>.PropName`, not the legacy
  `Spreadsheet.alias` form.

## References

- FreeCAD Parts Library:
  <https://github.com/FreeCAD/FreeCAD-library>
- build123d documentation:
  <https://build123d.readthedocs.io/>
- ISO 4762 (M3 cap screw), NEMA ICS 16-2001 (stepper mounts).
