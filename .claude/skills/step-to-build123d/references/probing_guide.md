# Reading probe_step.py output

The probe produces a JSON summary. This guide explains how to translate fields into build123d feature decisions.

## Top-level fields

**`is_assembly`, `solid_count`** — If `is_assembly` is true, the STEP contains multiple separate solids. Decide with the user whether to model them as one Compound or pick one. The skill is calibrated for single-solid parts; assemblies should be approached one solid at a time.

**`volume`** — Useful as a sanity check after you've drafted code. It also gives you a feel for scale.

**`bounding_box`** — `size` tells you the overall envelope. `center` near the origin suggests the part was modeled centered; far from origin suggests a different reference point. Builder mode primitives default to centered, so if `center ≠ origin` you'll need `align=` parameters or explicit translation.

## classified_features (the most actionable section)

The probe attempts to classify cylindrical features into four buckets. **Trust this classification first** before falling back to raw radius lists.

### `corner_fillets`

Cylindrical surfaces whose axes lie on the vertical edges of the bounding box, at distance ≈ √2·R from a corner. These are the rounded vertical edges of a prismatic part.

Translation:
```python
fillet(part.edges().filter_by(Axis.Z), radius=R)
```

(Adjust `Axis.Z` to whichever direction the corner-fillet axes are aligned with — check `axis_direction`.)

If `count >= 4` and `looks_like_pattern: true`, all vertical corners are filleted. Apply once at the end.

### `holes`

Cylindrical surfaces whose normals point toward the axis (material is outside). Each entry has:
- `radius` — the bore radius. Use `Cylinder(radius, depth, mode=Mode.SUBTRACT)` or `Hole(radius)`.
- `axis_direction` — the drill direction. `[0,0,1]` means drilled along +Z.
- `axis_origins` — the (x,y,z) of each hole's axis start.
- `count`, `looks_like_pattern` — hint at using `PolarLocations` / `GridLocations`.
- `inferred_depth_range.from_face_area` — depth(s) computed from cylindrical surface area / (2πr). Reliable when the cylinder is a clean tube.

To select a pattern:
- All on a circle (equal distance from a central point) → `PolarLocations(radius, count)`.
- Rectangular grid (equal X and Y spacing) → `GridLocations(x_pitch, y_pitch, x_count, y_count)`.
- Irregular → `Locations((x1,y1,z1), (x2,y2,z2), ...)`.

The probe doesn't pick the pattern type for you — compute distances and angles from `axis_origins` to decide.

### `bosses`

Same shape as holes but with normals pointing outward (material is inside the cylinder). These are extruded cylindrical features sticking out of the part.

Translation: `Cylinder(radius, height)` placed at `axis_origins`, height comes from `inferred_depth_range.from_face_area`.

A typical case: a pilot boss in front of a motor shaft, or a stepped shaft with multiple bosses.

### `other_cylinders`

Anything that didn't fit the other categories. Common cases:

- **The outer surface of a cylindrical or annular part.** The radius will be large relative to part size, and there's usually just one such face. Translation: this is the main `Cylinder(...)` body of the part — not a feature to add, it's the base shape.
- **Cylinders with non-principal axes.** Tilted features that don't align with X/Y/Z aren't classified.

When you see entries in `other_cylinders`, look at radius and count. A single large-radius entry → main body. Multiple small entries with weird positions → probably bosses or holes the heuristic missed; double-check by inspecting `axis_origins`.

## distinct_radii

A cross-section of all distinct radii values:

- **`corner_fillet_radii`** — radii of corner-fillet groups. Use directly with `fillet(...)`.
- **`hole_or_boss_radii`** — combined hole and boss radii (each separately listed in `classified_features` with its role).
- **`likely_3d_fillet_radii`** — radii present only as circular **edges**, not as cylindrical faces. These are typically 3D fillets between non-parallel surfaces (the surface is a torus, not a cylinder).

**Caveat for `likely_3d_fillet_radii`:** when a fillet of radius `r` is applied to a circular edge of radius `R`, it produces new circular edges of radius `R±r`. These derived radii will appear here even though they're not independent fillet operations.

Example: a flange with R40 outer cylinder, 4× R4 bolt holes, and R2 edge fillets, will produce `likely_3d_fillet_radii: [2, 6, 12, 38]` — only the `2` is a real fillet parameter; the others are derived.

**Heuristic:** the smallest value is most likely the real fillet radius. Larger values that equal `R ± smallest` for some R already in `hole_or_boss_radii` are derived. When in doubt, try the smallest as a single fillet radius and verify.

## face_inventory

**`counts_by_type`** — A quick fingerprint of part complexity:
- Mostly `PLANE` + a few `CYLINDER` → prismatic part with holes. The skill is well calibrated for this.
- `CYLINDER` dominates → mostly round part (shaft, flange, fitting).
- `BSPLINE` or `BEZIER` present → free-form surfaces. Warn the user.
- `TORUS` present → fillets between non-parallel surfaces, or genuine toroidal features.
- `CONE` present → tapered features or chamfers.

**`cylinder_faces`** — Full detail for every cylinder face. Used by the classifier; you usually don't read this directly.

**`planes_by_normal`** — Planes grouped by their normal direction. Two large planes with opposite normals along Z is the top and bottom of a prismatic part. Can help identify reference faces.

## edge_inventory

**`circle_radii_distinct_sorted`** — Cross-reference with `cylinder_face_radii`:
- Present in both lists → a cylindrical surface (hole or boss) — already classified.
- Only in edges → a fillet radius (or a derived fillet — see caveat above).

**`line_length_distinct_sorted`** — Distinct edge lengths. For a simple box you'll see three values matching the bounding box dimensions. Useful for cross-checking inferred dimensions: an edge length of 4.5 mm probably matches a hole's depth or a small dimension.

## symmetry_hints

Cheap heuristics to seed your modeling strategy:

- `centered_on_origin: true` → use centered primitives.
- `bbox_xy_square: true` → 4-fold symmetry candidate. Useful for square footprint parts and 4-count grid/polar patterns.
- All three squareness flags true → cubic envelope.

Hints, not facts. A square footprint doesn't guarantee 4-fold symmetry — verify with the diff.

## When the probe finds nothing useful

If the face counts are dominated by `BSPLINE`, `BEZIER`, or `OTHER`, the probe can't help much. Tell the user the part is outside the skill's design envelope.
