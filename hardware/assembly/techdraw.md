# build123d Assembly Technical Drawing — Agent Guide

Generate orthographic + isometric SVG drawings from a multi-part build123d
assembly. Read fully before writing code.

---

## Workflow

Three layers:

1. **Each part defines `Joint`s** at mating surfaces, inside `BuildPart`.
2. **`connect_to()`** positions parts via paired joints.
3. **`Compound(children=[...])`** freezes the assembled tree as one `Shape`.

A `Compound` IS a `Shape`, so `project_to_viewport` works on it just like a
single `Part`. Project 4 views (top / front / side / iso), translate each onto
an A4 sheet, export as layered SVG.

---

## Joints (the right way to assemble)

Joints come in mating pairs. Each is bound to a part with a unique `label`;
`connect_to()` aligns the second part to the first.

| Joint | DoF | Connects to | Use |
|---|---|---|---|
| `RigidJoint` | 0 | `RigidJoint` | Fixed mate |
| `RevoluteJoint` | 1 rot | `RigidJoint` | Hinge |
| `LinearJoint` | 1 trans | `RigidJoint`, `RevoluteJoint` | Slider |
| `CylindricalJoint` | 2 | `RigidJoint` | Screw |
| `BallJoint` | 3 rot | `RigidJoint` | Gimbal |

`RigidJoint` is the universal partner — expose one at every mating surface.

**Auto-binding:** joints created inside a `BuildPart` context don't need
`to_part`; the builder binds them on exit.

**`connect_to()` is a one-time reposition, NOT a binding.** To preserve
relative positions, wrap parts in a Compound, fuse them, or build them in the
same `BuildPart` context.

```python
with BuildPart() as bracket:
    Box(40, 40, 10)
    RigidJoint("motor_mount", joint_location=Location((0, 0, 10)))

with BuildPart() as motor:
    Cylinder(radius=21, height=40)
    RigidJoint("base", joint_location=Location((0, 0, -20), (180, 0, 0)))

bracket.part.joints["motor_mount"].connect_to(motor.part.joints["base"])
bracket.part.label, motor.part.label = "bracket", "nema17"
assembly = Compound(label="subassy", children=[bracket.part, motor.part])
```

Kinematic joints take an extra parameter:
```python
hinge.joints["pin"].connect_to(leaf.joints["pin"], angle=45)
slot.joints["rail"].connect_to(slide.joints["mount"], position=12)
screw.joints["axis"].connect_to(nut.joints["mount"], position=3, angle=120)
```

Out-of-range values raise `ValueError`. Visualize with
`show(..., render_joints=True)` in ocp-vscode.

---

## Key APIs

```python
Shape.project_to_viewport(
    viewport_origin, viewport_up=(0,0,1),
    look_at=None,        # default: shape center
    focus=None,          # None = orthographic (correct for tech drawings)
) -> (visible_edges, hidden_edges)

ExportSVG(unit=Unit.MM, scale=1, margin=0, line_weight=0.09, ...)

section(obj, section_by=Plane, mode=Mode.INTERSECT) -> Part
```

---

## The 5 Rules

**R1 — Joints over hand-set Locations.** Joints encode design intent.
Use `Pos * Rot * part` only for one-offs.

**R2 — `copy.copy()` for repeated fasteners.** Per Assemblies docs: 100 screws
via `deepcopy` = 52 MB / 5 s; via `copy.copy` = 550 KB / 0.25 s. Shallow copies
share CAD geometry but carry independent locations and labels.

**R3 — Label every child.** `part.label = "..."`. Labels survive into
`show_topology()`, STEP export (preserves color + label), and FreeCAD import.

**R4 — Orthographic, drop hidden on iso.** Leave `focus=None`. Internal mating
edges inflate the hidden set — drop them from the isometric, keep on the three
orthographics. For clean silhouettes, `Compound(...).fuse()` for projection
only; keep the original Compound for STEP export.

**R5 — Pick ONE scale pathway.** Either `ExportSVG(scale=...)` (single view
fills page) or per-view `scale_factor` in the projection helper (multi-view
A4 sheet). Never both. Derive from the bounding box:

```python
bbox = assembly.bounding_box()
max_dim = max(bbox.size.X, bbox.size.Y, bbox.size.Z)
drawing_scale = 0.5 if max_dim > 200 else 1.0
cam = max_dim * 4    # ≥ 3-4× bbox for near-orthographic look
```

---

## Standard Views

| View | `viewport_origin` | `viewport_up` |
|---|---|---|
| Isometric | `(cam, cam, cam)` | `(0, 0, 1)` |
| Plan (top) | `(0, 0, cam)` | `(0, 1, 0)` |
| Front | `(0, -cam, 0)` | `(0, 0, 1)` |
| Right side | `(cam, 0, 0)` | `(0, 0, 1)` |

`look_at` omitted (default = shape center).

---

## Script Template

```python
import copy
from datetime import date
from build123d import *

# 1. Build parts with joints, connect_to, wrap in Compound.
def build_assembly() -> Compound:
    # with BuildPart() as part_a:
    #     ...
    #     RigidJoint("mate", joint_location=Location(...))
    # part_a.part.joints["mate"].connect_to(part_b.part.joints["mate"])
    # part_a.part.label = "..."
    # return Compound(label="my_assembly", children=[part_a.part, ...])
    ...

# 2. Project helper (verbatim from tech drawing tutorial).
def project_to_2d(part, origin, up, page_origin, scale_factor=1.0):
    scaled = part if scale_factor == 1.0 else scale(part, scale_factor)
    visible, hidden = scaled.project_to_viewport(origin, up, look_at=(0,0,0))
    visible = [Pos(*page_origin) * e for e in visible]
    hidden  = [Pos(*page_origin) * e for e in hidden]
    return ShapeList(visible), ShapeList(hidden)

# 3. Setup.
assembly = build_assembly()
bbox = assembly.bounding_box()
max_dim = max(bbox.size.X, bbox.size.Y, bbox.size.Z)
ds = 0.5 if max_dim > 200 else 1.0
cam = max_dim * 4

border = TechnicalDrawing(
    page_size=PageSize.A4, drawing_scale=ds,
    title="...", sub_title=f"Scale 1:{int(1/ds)}",
    designed_by="...", design_date=date.today(),
    drawing_number="...", sheet_number=1,
)
page = border.bounding_box().size
visible, hidden = [], []

# 4. Project each view (skip hidden on iso).
iso_v, _ = project_to_2d(assembly, (cam,cam,cam), (0,0,1),
                         (page.X*0.25, page.Y*0.25), ds)
visible.extend(iso_v)
# ... plan / front / side similarly, extending both lists ...

# 5. Export.
exporter = ExportSVG(unit=Unit.MM)
exporter.add_layer("Visible")
exporter.add_layer("Hidden", line_color=(99,99,99), line_type=LineType.ISO_DOT)
exporter.add_shape(visible, layer="Visible")
exporter.add_shape(hidden,  layer="Hidden")
exporter.add_shape(border,  layer="Visible")
exporter.write("assembly_drawing.svg")

export_step(assembly, "assembly.step")   # preserves labels + colors
```

---

## Cross-Section (optional)

```python
cut = section(assembly, section_by=Plane.XY.offset(50))
# Project like other views, or add cut.faces() directly for filled silhouette.
```

---

## When NOT to use build123d for drafting

For section hatching, BOM tables, balloon callouts, multi-sheet drawings, or
GD&T frames — export STEP and use FreeCAD TechDraw. `export_step` preserves
the assembly tree, labels, and colors.

---

## Checklist

- [ ] Mating surfaces defined as `RigidJoint`s inside `BuildPart`
- [ ] Parts positioned via `connect_to()`, not hand-set Locations
- [ ] Joint labels unique within each part
- [ ] `connect_to()` called BEFORE wrapping in Compound
- [ ] Every Compound child has `.label`
- [ ] Repeated fasteners use `copy.copy()`
- [ ] `drawing_scale` and `cam` derived from bounding box
- [ ] `focus=None` (orthographic)
- [ ] Iso drops hidden; orthographics keep them
- [ ] Not double-applying `ExportSVG.scale` and `scale_factor`
- [ ] SVG has Visible + Hidden layers
- [ ] `export_step(assembly, ...)` called

---

## Reference

- Joints: https://build123d.readthedocs.io/en/latest/joints.html
- Joint tutorial: https://build123d.readthedocs.io/en/latest/tutorial_joints.html
- Assemblies: https://build123d.readthedocs.io/en/latest/assemblies.html
- Technical drawing tutorial: https://build123d.readthedocs.io/en/latest/tech_drawing_tutorial.html
- Import/Export: https://build123d.readthedocs.io/en/latest/import_export.html
- Operations: https://build123d.readthedocs.io/en/latest/operations.html