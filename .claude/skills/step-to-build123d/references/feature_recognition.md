# From geometry to build123d features

The probe gives you raw measurements. This guide maps those measurements onto the build123d feature you should generate. The hard part of reverse engineering is rarely "what dimension" — the probe gives you that — but "which feature created it."

## The cylindrical face problem

A `GeomType.CYLINDER` face is the single most ambiguous probe output. It can be:

1. **A hole** (cylindrical hole through or into the part).
2. **A boss** (cylindrical extrusion sticking out).
3. **A fillet surface** (when the fillet is between two perpendicular planes — the resulting fillet surface is a quarter cylinder).
4. **The outer surface of a round part** (a shaft, a pin, a flange's outer rim).

How to tell them apart:

- **Outer surface of a round part:** radius is large relative to part bounding box (e.g., radius is half the bbox X). Axis usually passes through the part center. There's typically just one such face.
- **Hole:** radius is small relative to bbox. Axis passes through interior of the part. The cylindrical face is bounded by two circular edges of the same radius (entry and exit, or entry and bottom for a blind hole). The face's surface normal points inward.
- **Boss:** similar to hole geometrically (axis interior, small-ish radius), but bounded by circular edges that connect to the *outside* of a planar face, not the inside. The face's surface normal points outward.
- **Fillet quarter-cylinder:** small radius, short length along the axis, area is small. Axis runs along an edge of the part rather than through its body. **The probe's `classified_features.corner_fillets` already separates these out** — trust the classification first; if it shows a corner-fillet group, use it.

When in doubt, look at `classified_features` directly: holes, bosses, corner_fillets, and other_cylinders are pre-sorted for you. If the classifier put something in `other_cylinders`, that's where you need to think harder.

## The radius classification trick

The probe's `distinct_radii.likely_3d_fillet_radii` field uses a useful invariant: a 3D fillet between non-parallel surfaces has a toroidal (not cylindrical) surface bounded by circular edges, so its radius shows up in `circle_edge_radii` but not in `cylinder_face_radii`. The probe surfaces these radii separately for fillet-on-edge identification.

So:
- **Radius in both `cylinder_face_radii` and `circle_edge_radii`** → hole or boss.
- **Radius only in `circle_edge_radii`** → likely a fillet.
- **Radius only in `cylinder_face_radii`** → unusual; might be a non-through cylindrical surface, or a fillet that's visible as a face but bounded by non-circular edges.

This is a heuristic, not a proof. Verify with the diff.

## Feature-to-API cheatsheet

**Style note:** when a part is essentially a 2D profile extruded along an axis, prefer `BuildSketch + extrude` over stacking 3D primitives. That's the official build123d idiom (per the "2D before 3D" tip). Use the 3D primitive table below for simple bases, but the moment you have multiple coplanar features (a rectangle with several holes, a complex outline), build them in a sketch first.

### Solid primitives

| Geometric pattern | build123d |
|---|---|
| Rectangular volume, axis-aligned | `Box(L, W, H)` |
| Rectangular volume, rotated | `Box(L, W, H)` then translate/rotate, or sketch + extrude |
| Cylindrical volume, axis-aligned to Z | `Cylinder(radius, height)` |
| Cylindrical volume, other axis | `Cylinder(radius, height, rotation=(90, 0, 0))` for axis-Y, or sketch on `Plane.XZ`/`Plane.YZ` + extrude |
| Sphere | `Sphere(radius)` |
| Cone or frustum | `Cone(bottom_radius, top_radius, height)` |
| Torus | `Torus(major_radius, minor_radius)` |
| Hex prism / regular prism | `extrude(RegularPolygon(radius, side_count), amount=h)` |

### Holes

`Hole(radius, depth)` is the official primitive for drilled features. It's already subtractive (`Mode.SUBTRACT` is the default), so you don't write the mode. Always prefer `Hole` to manual `Cylinder + Mode.SUBTRACT`:

```python
# Preferred — concise, intent-clear
with BuildPart() as p:
    Box(50, 50, 10)
    with Locations(p.faces().sort_by(Axis.Z)[-1]):
        Hole(radius=5, depth=10)   # depth optional; through-hole if omitted in some contexts

# Acceptable but verbose — fall back to this only when Hole's behavior doesn't fit
with BuildPart() as p:
    Box(50, 50, 10)
    Cylinder(5, 10, mode=Mode.SUBTRACT)
```

`CounterBoreHole(radius, counter_bore_radius, counter_bore_depth)` and `CounterSinkHole(radius, counter_sink_radius)` are equivalents for stepped fastener holes — use them when the probe's `classified_features.holes` shows two stacked entries with different radii at the same axis position.

For non-axis-aligned holes (e.g., drilled through a vertical wall), do the placement on the wall's face:

```python
wall_face = part.faces().sort_by(Axis.Y)[-1]   # outer wall face
with Locations(wall_face):
    with GridLocations(31, 31, 2, 2):
        Hole(radius=1.5, depth=5)
```

`Locations(face)` makes the face the local working plane; everything inside gets oriented to it.

### Patterns

| Probe finding | Pattern API |
|---|---|
| N axis-origins on a circle, equal angular spacing | `with PolarLocations(radius, count, start_angle=θ): ...` |
| N axis-origins on a rectangular grid | `with GridLocations(x_pitch, y_pitch, x_count, y_count): ...` |
| Hexagonal arrangement (e.g., for a fastener pattern) | `with HexLocations(apothem=a, x_count=n, y_count=m): ...` |
| Irregular set | `with Locations((x1,y1), (x2,y2), ...): ...` |

To check if hole positions form a polar pattern: compute distances from each `axis_origin` to a candidate center (often the bbox center). If they're all equal (within tolerance), it's polar. Compute angles to determine `start_angle`.

To check for a grid: project positions onto X and Y axes. If they form a regular grid (equal spacing in both directions), it's `GridLocations`.

### Fillets and chamfers

Always apply at the end, after the topology is stable:

```python
# Round all top edges
fillet(part.edges().group_by(Axis.Z)[-1], radius=2)

# Chamfer the edges of a specific cylindrical hole
hole_edges = part.edges().filter_by(GeomType.CIRCLE).filter_by(lambda e: e.radius == 4)
chamfer(hole_edges, length=0.5)

# Fillet the vertical edges of a box (Axis.Z direction)
fillet(part.edges().filter_by(Axis.Z), radius=1)
```

Selectors are how you target the right edges. `filter_by(Axis.Z)` selects edges parallel to Z. `filter_by(GeomType.CIRCLE)` selects circular edges. `group_by(Axis.Z)[-1]` selects the highest group along Z. Combine these.

If you have multiple distinct fillet radii, apply them in separate `fillet()` calls — one per radius — with selectors that target only the right edges. The probe's `classified_features.corner_fillets` (for prism corners) and `distinct_radii.likely_3d_fillet_radii` (for 3D fillets between surfaces) directly drive the structure of your code.

### Revolved features

If the part is a body of revolution (the probe shows mostly cylinder/cone/torus faces and possibly a small number of planes), use `revolve`:

```python
with BuildPart() as part:
    with BuildSketch(Plane.XZ) as profile:
        with BuildLine() as ln:
            Polyline((0, 0), (20, 0), (20, 5), (10, 5), (10, 30), (0, 30))
            Line((0, 30), (0, 0))  # close
        make_face()
    revolve(axis=Axis.Z)
```

Reverse engineering a revolved part from the probe: identify the axis of rotation (the common axis of the cylindrical faces), then sketch the cross-section profile in a plane containing that axis. The probe doesn't extract the profile for you — you'll need to read radii at different heights and build the polyline.

### Extruded prismatic features

For a part that's a 2D shape extruded along Z:

```python
with BuildPart() as part:
    with BuildSketch() as sk:
        Rectangle(50, 30)
        with Locations((10, 10)):
            Circle(3, mode=Mode.SUBTRACT)
    extrude(amount=10)
```

The probe will tell you: the bbox Z size = the extrusion depth, the top and bottom planar faces (with opposite normals along Z) define the start and end planes, and all the side faces are perpendicular to the top/bottom — so the whole thing came from a 2D sketch.

## Sanity heuristics

Before generating code, ask:

1. **Can this part be built from one base primitive plus subtractions?** If so, that's the simplest structure. Resist generating an over-complex script.
2. **Is there obvious symmetry?** If yes, model one half/quarter and use `mirror`. The probe's `symmetry_hints` is a starting point, but visual inspection of the part is usually decisive.
3. **What's the natural reference plane?** Picking the right `Plane.XY` / `Plane.XZ` / `Plane.YZ` for your initial sketch makes the rest of the code line up cleanly.

When in doubt, simpler is better. A draft that's 80% right with simple structure is easier to fix than one that's 95% right with tangled feature dependencies.
