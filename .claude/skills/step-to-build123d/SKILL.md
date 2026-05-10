---
name: step-to-build123d
description: Reverse-engineer a STEP file into parametric build123d Python code, with a measure-driven workflow that uses geometry probing and fingerprint-based verification (volume, bounding box, edge-length and face-area multisets) to converge on a faithful replica. Use this skill whenever the user provides a .step or .stp file and asks for build123d code, asks to "convert/recreate/replicate/reproduce/reverse-engineer/parametrize" a STEP file, mentions wanting CAD-as-code from an existing model, or asks how to turn a STEP into a Python script — even if they don't explicitly name build123d. Also use when the user mentions wanting parametric versions of imported geometry. The skill is calibrated for simple parts (boxes, cylinders, holes, patterns, fillets, chamfers, revolved/extruded profiles) and includes a verification loop that catches dimension errors before the user sees them.
---

# STEP → build123d Reverse Engineering

This skill turns a STEP file into clean parametric build123d code through a **measure-first, verify-always** workflow. It is built around one core insight: STEP files are dead-end B-rep output — the parametric design intent is gone — so you must rebuild that intent from geometric measurements rather than guesswork.

The skill is calibrated for **simple parts**: prismatic shapes, cylinders, holes, hole patterns, fillets/chamfers, simple revolves and extrusions. For free-form surfaces, sheet-metal unrolling, or large assemblies, follow the simple-part workflow but warn the user that perfect closure may not be achievable and offer a best-effort with explicit caveats.

## PhysiClaw integration (target output layout)

In this repo the output is **not** a flat script — it's a part directory under `hardware/parts/<part>/` with three files:

```text
hardware/parts/<part>/
├── __init__.py        empty marker
├── spec.py            @dataclass(frozen=True) inheriting from parts.Spec — pure dimensions
└── b3d.py             StandardPart subclass with build(); exports PART = MyPart(MY_SPEC)
```

Read `hardware/README.md` (especially "Adding a part" and "Conventions") before writing code — it pins naming, units, and the discovery contract. The short version:

- Dimensions live **only** in `spec.py`, with units in field names (`outer_diameter_mm`, `width_mm`). `b3d.py` reads them as `self.spec.foo` — no module-level constants and no literals duplicated from the spec.
- `b3d.py` subclasses `parts.StandardPart`, sets `output_name` (PascalCase part designator like `Bearing_608`, `NEMA17`, `Extrusion_2020_L300`), implements `build()` which **returns a `build123d.Part`** (the driver does the export), and exposes a module-level `PART = MyPart(MY_SPEC)`.
- Do not call `export_step` inside `build()` and do not write driver code — `hardware/scripts/build_all_b3d.py` discovers the module, calls `PART.build()`, and writes to `hardware/output/step/b3d/<output_name>.step` and `hardware/output/stl/<output_name>.stl`.
- A part directory may also contain `fc.py` (FreeCAD twin). This skill produces `b3d.py` only; if the part already has an `fc.py`, the README's cross-check section applies after you finish.
- Source STEP files don't have a fixed home in the repo. Treat the path the user gives you as the input; nothing in the project layout requires you to move it.

The "core loop" below applies as written, but Step 3 ("Draft") and Step 4 ("Verify") have PhysiClaw-specific notes — read them before generating files.

## The core loop

1. **Probe** the STEP file to extract a structured geometric summary (faces by type, edges by type, hole centers, bounding box, symmetries).
2. **Describe** the part in plain language — what features compose it — and confirm with the user before coding.
3. **Draft** the part directory: dimensions in `spec.py`, geometry in `b3d.py`, every literal in `b3d.py` reading from `self.spec`.
4. **Verify** by building the draft through `build_all_b3d.py` and running the verifier — primary check is a geometric fingerprint (volume, bounding box, edge-length and face-area multisets); boolean diff is a secondary visual aid.
5. **Iterate** on the specific failures the verifier identified. Don't rewrite from scratch.

Every step has a purpose. Skipping the probe means coding from screenshots and intuition (low success rate). Skipping verification means "looks right" gets shipped as "is right" — and CAD users will notice 0.5 mm off in seconds.

## Step 1: Probe the STEP

Run the probing script before doing anything else. In this repo, build123d is in the `cad` dependency group, so invoke it with `uv`:

```bash
uv run --group cad python .claude/skills/step-to-build123d/scripts/probe_step.py path/to/part.step
```

This produces a JSON summary covering:

- **`bounding_box`** — overall envelope and centering.
- **`face_inventory`** — counts by GeomType, plus full cylinder face details (radius, axis, area) and planes grouped by normal.
- **`edge_inventory`** — distinct circle radii and line lengths.
- **`distinct_radii`** — radii separated into `corner_fillet_radii`, `hole_or_boss_radii`, and `likely_3d_fillet_radii`. The classification is heuristic but usually correct for simple parts.
- **`classified_features`** — the most actionable section, with cylindrical features sorted into `corner_fillets`, `holes`, `bosses`, and `other_cylinders`. Holes and bosses include `inferred_depth_range.from_face_area` (height computed from cylindrical surface area / 2πr).
- **`symmetry_hints`** — bbox centering and squareness flags.

If the probe fails to import the STEP (rare, but happens with assemblies or exotic STEP variants), fall back to:

```python
from build123d import import_step
shape = import_step("part.step")
print(shape.bounding_box())
print(f"faces: {len(shape.faces())}, edges: {len(shape.edges())}, volume: {shape.volume:.3f}")
```

…and adapt from there. If it's an assembly (Compound with children), iterate `shape.children` and treat each solid separately.

## Step 2: Describe before coding

After probing, write a plain-language description of the part: "this is a Φ80×10 disc with a Φ20 central through-hole, four Φ8 holes on a Φ60 PCD, and R2 fillets on the outer top edge." Surface this to the user and ask whether the description matches their intent before generating code.

Why this matters: probe output can be ambiguous — a `GeomType.CYLINDER` face could be a hole, a boss, or a fillet surface. Your description forces you to commit to an interpretation, and the user can correct you cheaply at this stage. Correcting after 200 lines of generated code is much more expensive.

If the user provides screenshots alongside the STEP, look at them. Multi-view confirmation eliminates orientation ambiguities (which axis is "up", which face is "front") that probe data alone can't resolve.

## Step 3: Draft the build123d code

Follow these conventions when generating code. They are not stylistic preferences — they directly reflect build123d's official best practices and materially affect whether your code runs correctly.

### Code conventions

**Import build123d names explicitly.** The official build123d idiom is `from build123d import *`, but inside this repo the standard-parts library is library code, not a one-off script — existing `b3d.py` modules list the names they use. Match that style: import what you need by name from `build123d` (`BuildPart`, `BuildSketch`, `Circle`, `extrude`, `fillet`, etc.). Look at `hardware/parts/bearing_608/b3d.py` for the canonical shape.

**Use Builder context style** (`with BuildPart() as p:`) by default. It's the standard for stateful, multi-feature workflows. Algebra mode (`p = Box(...) + Cylinder(...)`) is for stateless one-liners; switch only if the user requests it.

**Define all dimensions in `spec.py` as a frozen dataclass.** Every length, radius, count, and spacing gets a field with a unit-suffixed name (`outer_diameter_mm`, `bolt_count` is OK because counts are dimensionless). `b3d.py` reads them as `self.spec.foo` — never inline literals, never module-level constants in `b3d.py`. This is what the PhysiClaw architecture means by "parametric" and is what lets a future `fc.py` cross-check against the same source of truth.

**Build complex parts by sketching a 2D profile first, then extruding or revolving**, rather than stacking 3D primitives. This is the single biggest difference between build123d-style code and OpenSCAD-style CSG code. Per the official tips: *"3D structures are much more intricate, and 3D operations can be slower and more prone to failure."* For purely cylindrical or boxy bases the 3D-primitive form (`Cylinder(...)`, `Box(...)`) is fine and idiomatic — but as soon as you have multiple holes or complex 2D features, sketch them together and extrude once.

### Worked example

A fictional Φ80 flange — central bore, four bolt holes on a 60 mm PCD, fillet on the top outer edge — under the PhysiClaw layout looks like this.

`hardware/parts/flange_80/__init__.py` is empty.

`hardware/parts/flange_80/spec.py`:

```python
"""Dimensions for the Φ80 mounting flange."""

from dataclasses import dataclass

from parts import Spec


@dataclass(frozen=True)
class Flange80(Spec):
    outer_diameter_mm: float
    thickness_mm: float
    center_hole_diameter_mm: float
    bolt_hole_diameter_mm: float
    bolt_pitch_circle_diameter_mm: float
    bolt_count: int
    edge_fillet_mm: float


FLANGE_80 = Flange80(
    outer_diameter_mm=80.0,
    thickness_mm=10.0,
    center_hole_diameter_mm=20.0,
    bolt_hole_diameter_mm=8.0,
    bolt_pitch_circle_diameter_mm=60.0,
    bolt_count=4,
    edge_fillet_mm=2.0,
)
```

`hardware/parts/flange_80/b3d.py`:

```python
"""Φ80 mounting flange — build123d twin."""

from build123d import (
    Axis,
    BuildPart,
    BuildSketch,
    Circle,
    GeomType,
    Mode,
    Part,
    PolarLocations,
    extrude,
    fillet,
)

from parts import StandardPart
from parts.flange_80.spec import FLANGE_80


class Flange80(StandardPart):
    output_name = "Flange_80"

    def build(self) -> Part:
        spec = self.spec
        with BuildPart() as bp:
            with BuildSketch():
                Circle(spec.outer_diameter_mm / 2)
                Circle(spec.center_hole_diameter_mm / 2, mode=Mode.SUBTRACT)
                with PolarLocations(spec.bolt_pitch_circle_diameter_mm / 2, spec.bolt_count):
                    Circle(spec.bolt_hole_diameter_mm / 2, mode=Mode.SUBTRACT)
            extrude(amount=spec.thickness_mm)

            top_edges = bp.edges().filter_by(GeomType.CIRCLE).group_by(Axis.Z)[-1]
            fillet(top_edges, spec.edge_fillet_mm)
        return bp.part


PART = Flange80(FLANGE_80)
```

Note what is **not** there: no `export_step`, no `if __name__ == "__main__"`, no driver code, no inline numeric literals. `build()` returns a `Part` and the discovery driver handles the rest. Compare with `hardware/parts/bearing_608/b3d.py` for the simplest possible shape of this pattern.

The example uses `Circle(..., mode=Mode.SUBTRACT)` *inside* a sketch to carve the bores out of the profile before extruding once. That's the 2D-first idiom. The `Hole` primitive below is for the *other* case — drilling into an already-extruded face.

### Use Hole, not Cylinder + SUBTRACT

`Hole(radius, depth)` is the official primitive for through and blind holes. It defaults to `Mode.SUBTRACT`, so you don't need to write it. Use it instead of `Cylinder(r, h, mode=Mode.SUBTRACT)`:

```python
# Preferred for hole drilling on a placed face
with Locations(part.faces().sort_by(Axis.Z)[-1]):
    with GridLocations(40, 30, 2, 2):
        Hole(radius=2.5, depth=6.0)
```

`CounterBoreHole(radius, counter_bore_radius, counter_bore_depth)` and `CounterSinkHole(radius, counter_sink_radius)` are the equivalents for stepped fastener holes — use them when the probe shows stepped cylindrical pairs.

### Locations come before objects

In Builder mode, **placement must be specified before the object is created.** A common bug is to write:

```python
Cylinder(5, 10).translate((20, 0, 0))   # WRONG: cylinder is at origin
```

The `Cylinder` is added to the builder at creation, and `.translate(...)` only modifies the temporary returned object. The fix is `with Locations(...)`:

```python
with Locations((20, 0, 0)):              # right
    Cylinder(5, 10)
```

This applies to `Locations`, `GridLocations`, `PolarLocations`, `HexLocations`. Use them instead of `.translate()`/`.move()`/`.moved()` chains.

### Selectors: top-down, not magic indices

`part.faces().sort_by(Axis.Z)[-1]` (the highest-Z face) is robust to topology changes; `part.faces()[7]` is not. Per the official tip: *"select an object from higher up in the topology first, then select the object from there."*

```python
top_face = part.faces().sort_by(Axis.Z)[-1]
hole_edges = top_face.edges().filter_by(GeomType.CIRCLE)
chamfer(hole_edges, length=1.0)
```

Common selectors: `filter_by(GeomType.CYLINDER)`, `filter_by(Axis.Z)` (parallel to Z), `sort_by(Axis.Z)`, `group_by(Axis.Z)[-1]`, `filter_by(lambda f: f.radius == 4.0)`.

### Fillets and chamfers go last

Apply at the end, after the topology is stable. Inline filleting during construction is brittle. If you have multiple distinct fillet radii, apply them in separate `fillet()` calls with selectors targeting only the right edges.

### Pattern primitives, not loops over translate

Per probe output, hole patterns map directly:

| Pattern | Builder code |
|---|---|
| Polar (N on a circle) | `with PolarLocations(radius, count): Hole(...)` |
| Rectangular grid | `with GridLocations(x_pitch, y_pitch, x_count, y_count): Hole(...)` |
| Hexagonal | `with HexLocations(apothem=a, x_count=n, y_count=m): Hole(...)` |
| Irregular | `with Locations((x1,y1), (x2,y2), ...): Hole(...)` |

### Common pitfalls

- **`BuildPart` is not a `Part`**: when you need the geometry (to return from `build()`, or to export), use `bp.part`. `bp` itself is the context-manager builder; `bp.part` is the `Part` it produced. Same shape for sketches: `bs.sketch`.
- **Radius vs diameter**: `Cylinder(radius, height)` and `Hole(radius, depth)` take **radius**, not diameter. Surprising number of bugs come from this.
- **Default alignment is CENTER**: a `Box(L, W, H)` is centered on origin. If the original has corner at origin, use `align=(Align.MIN, Align.MIN, Align.MIN)`.
- **`GeomType` is an enum**: `filter_by(GeomType.CYLINDER)`, not `filter_by("CYLINDER")`.
- **`BuildSketch(Plane.XZ)` looks wrong but isn't**: sketches always render on local `Plane.XY` first, then get placed on the workplane. Selectors inside the sketch operate on local XY coordinates, not the final placed coordinates. If you need to select features in the placed sketch, do it after `extrude()` returns, on the resulting Part.
- **`BuildLine` inside `BuildSketch` should not specify a different plane**: it'll be reoriented to the sketch's local Plane.XY anyway. Just write `with BuildLine():` without a plane argument when nested in a sketch.
- **Nested Builders don't inherit the parent's workplane**: every `BuildPart`, `BuildSketch`, `BuildLine` defaults to `Plane.XY`. If you want a different plane, pass it explicitly.

## Step 4: Verify with geometric fingerprint

`verify_match.py` performs two complementary checks:

- **Geometric fingerprint** (primary): compares volume, bounding box size, the multiset of all edge lengths, and the multiset of all face areas. For simple parts these four signatures collectively form a near-unique identifier — two parts with matching fingerprints are functionally identical. Robust to STEP round-trip artifacts; this is the source of truth.
- **Boolean diff** (secondary): tries `original - draft` and `draft - original`, exporting the diff geometries when meaningful. Useful when it works because you can visualize what's wrong, but OCCT booleans across separately-imported STEPs sometimes degenerate silently. The script detects degeneration and marks the boolean result unreliable. **When boolean is unreliable, trust the fingerprint.**

In PhysiClaw, *building* the draft means running the project's discovery driver, which writes the STEP to a known path. Then point the verifier at it:

```bash
# Build all b3d parts (yours plus the existing library — discovery is automatic)
uv run --group cad python hardware/scripts/build_all_b3d.py

# Verify your draft against the user-supplied original
uv run --group cad python .claude/skills/step-to-build123d/scripts/verify_match.py \
    path/to/original.step \
    hardware/output/step/b3d/<output_name>.step
```

`<output_name>` is whatever string you set on the `StandardPart` subclass (e.g. `Flange_80` → `hardware/output/step/b3d/Flange_80.step`). If `build_all_b3d.py` prints `ERR <module>: …` for your part, fix that before reading the verifier output — a missing STEP isn't a fingerprint mismatch.

Interpreting fingerprint results:

- **All four match**: success.
- **Volume differs but bounding box matches**: a feature is wrong-sized (hole too large/small, fillet wrong radius, missing or extra cut). The edge-length and face-area mismatches will tell you which one — large area differences point to large features (a bore diameter), small ones to small features (a fillet radius).
- **Bounding box differs**: an overall dimension is wrong. Fix this first; everything else cascades.
- **Edge or face count differs**: a feature is missing or extra entirely. Re-examine the feature list, don't tweak parameters.
- **Counts match but values differ**: dimensions are off. The `max_diff_at` field shows the worst-mismatching value pair — work out what feature it corresponds to.

For more on iterating against the diff, see `references/diff_iteration.md`.

## Step 5: Iterate, don't rewrite

When the verifier reports a mismatch, change only the relevant parameter or feature. If the bolt-circle radius is off by 1 mm, change `bolt_pitch_circle_diameter_mm` in `spec.py` and rerun the build + verify — don't regenerate the whole module. This is what parametric code is for.

Dimension errors are spec-only changes; topology errors (a missing fillet, an extra pocket) live in `b3d.py`. Keep that split: don't bake numeric literals into `b3d.py` while iterating, even temporarily.

If after three iterations the fingerprint isn't converging, stop and re-probe. Something about the part's structure was misread the first time, and continuing to tweak parameters won't fix a structural error.

### If the part already has an `fc.py`

If you're adding `b3d.py` to a part directory that already contains `fc.py` (rare in this skill's flow, but possible), follow the cross-check section in `hardware/README.md` after the verifier passes: build both backends, import both STEPs, and compare volume / bounding box / face count. The user-supplied STEP is the reference for **your** draft only — the existing `fc.py` is a separate source of truth that may itself disagree with the original (the README documents this exact scenario for the 2020 extrusion). Surface the discrepancy rather than silently making one match the other.

## When the part is genuinely complex

The skill is calibrated for simple parts. If probing reveals any of:

- `GeomType.BSPLINE` or `GeomType.BEZIER` faces (free-form surfaces)
- More than ~30 distinct cylinder radii or face-area bins (a busy feature tree)
- Sheet-metal characteristics (thin uniform thickness, bend regions)
- Multiple disconnected solids (`is_assembly: true` in the probe output)

…then warn the user up front: a faithful build123d replica may not be achievable. Offer a best-effort approximation (parametric primitives that match the bounding shape) and ask whether they want the verification step run or skipped given that closure isn't expected.

Don't silently skip verification on complex parts — the user should know what guarantees you're providing.

## Quick reference

| Need | API |
|---|---|
| Import STEP | `from build123d import import_step; s = import_step("f.step")` |
| Return Part from `build()` | `return bp.part` (driver exports it) |
| Ad-hoc export (debugging) | `export_step(bp.part, "f.step")` |
| Bounding box | `s.bounding_box()` → has `.size`, `.center()`, `.min`, `.max` |
| Volume | `s.volume` |
| Drill a hole | `Hole(radius, depth)` (subtractive by default) |
| Counterbore | `CounterBoreHole(radius, counter_bore_radius, counter_bore_depth)` |
| Countersink | `CounterSinkHole(radius, counter_sink_radius)` |
| Boolean subtract (algebra) | `a - b` |
| Sketch on a face | `with BuildSketch(part.faces().sort_by(Axis.Z)[-1]):` |
| Place on a face | `with Locations(part.faces().sort_by(Axis.Z)[-1]):` |
| Filter cylindrical faces | `s.faces().filter_by(GeomType.CYLINDER)` |
| Filter circular edges | `s.edges().filter_by(GeomType.CIRCLE)` |
| Sort by axis position | `.sort_by(Axis.Z)[-1]` for top, `[0]` for bottom |
| Group by axis | `.group_by(Axis.Z)[-1]` for highest group |
| Filter by lambda | `.filter_by(lambda f: f.radius == 4.0)` |
| Polar pattern | `with PolarLocations(radius, count): Hole(...)` |
| Grid pattern | `with GridLocations(x_pitch, y_pitch, x_count, y_count): Hole(...)` |
| Fillet edges | `fillet(part.edges().filter_by(Axis.Z), radius=R)` |
| Chamfer edges | `chamfer(edges, length=L)` |
| Revolve | `revolve(axis=Axis.Z)` after a sketch |
| Extrude (cut) | `extrude(amount=-h, mode=Mode.SUBTRACT)` |
| Mirror | `mirror(about=Plane.YZ)` |

For deeper API questions, the build123d docs are at https://build123d.readthedocs.io/ — the Selector Tutorial and Filter Examples pages are especially useful for matching geometry queries to feature recognition.

---

The reference files in `references/` contain expanded guidance:
- `references/probing_guide.md` — what each probe field means and how to interpret edge cases
- `references/diff_iteration.md` — diagnosing common boolean-diff patterns
- `references/feature_recognition.md` — turning probe output into build123d feature choices (when is a cylindrical face a hole vs. a boss vs. a fillet)
