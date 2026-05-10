# Diagnosing verify_match.py output

After verification fails, the fingerprint values are the primary diagnostic. The boolean diff is a useful supplement when it works, but it can degenerate silently across STEP round-trips (the script flags this with `reliable: false`). When the boolean is unreliable, the fingerprint is your full picture.

This guide maps fingerprint patterns to root causes and concrete fixes.

## Reading the fingerprint

The script reports four signatures: volume, bounding box, edge-length multiset, face-area multiset. Each can independently match or mismatch, and the pattern of which ones match is highly diagnostic.

### Pattern: bounding box differs

Fix this before anything else. A wrong overall envelope cascades into spurious mismatches everywhere.

Common causes:

- **Wrong primary dimension.** Disc OD off, box length off. The `original_size` vs `draft_size` arrays show exactly which axis.
- **Coordinate-system shift.** If the original was modeled with a corner at the origin and your draft is centered (or vice versa), the bounding box is the same size but `volume` will match anyway. This won't fail the bbox-size check — but it might fail the boolean diff. If fingerprint passes but boolean reports both diffs equal to part volume, suspect placement.
- **Wrong units.** Off by 25.4× → inches vs mm. Off by 1000× → m vs mm.

### Pattern: bounding box matches, volume differs

The overall envelope is right, but material content is wrong somewhere.

- **Volume diff < 1%**: a small feature is wrong — fillet radius, chamfer size, small hole diameter. Look at the smallest-area mismatches in the face fingerprint.
- **Volume diff 1–10%**: a medium feature is wrong — a bore diameter, a pocket depth, a missing small hole.
- **Volume diff > 10%**: a major feature is missing or grossly wrong.

The sign tells you which side: if `draft_volume > original_volume`, your draft has extra material (missing a hole, smaller bore, missing pocket). If less, your draft has missing material (extra hole, oversized cut).

### Pattern: edge or face count differs

Counts differing means **a feature is missing or extra**, not just wrong-sized. Don't tweak parameters; rebuild the feature list.

The probe summary you started with is the source of truth — re-read it. Did you account for every entry in `classified_features.holes`, `bosses`, and `corner_fillets`? Every distinct value in `hole_or_boss_radii`? Is there a feature you classified incorrectly (a boss treated as a hole, a corner fillet missed)?

A useful check: re-run `probe_step.py` on your draft and compare the face-type counts to the original's. If the original has 6 cylinder faces and 2 plane faces, but your draft has 4 cylinder and 4 plane, you're missing two cylindrical features.

### Pattern: counts match, values differ

The structure is right, dimensions are off. The `max_diff_at` field gives you the worst-mismatching pair — `[original_value, draft_value]`. Work out what feature has that dimension:

- An edge length of `2π × R` is the perimeter of a circular edge of radius R. So `max_diff_at: [25.13, 18.85]` translates to "you have a R3 circle where there should be R4" (since 2π×4≈25.13 and 2π×3≈18.85).
- A face area near `π × R²` is a circular face. `max_diff_at: [50.27, 28.27]` → R4 vs R3.
- A face area near `2π × R × H` is a cylindrical surface. The value tells you radius × height; the smaller dimension is usually the one that's off.
- For non-circular features, you may need to cross-reference: a 78 mm vs 80 mm edge length is probably an outer dimension or a polygon side.

When this pattern persists across iterations, you're tweaking the right feature but reading the wrong dimension from the original. Go back to the probe output for that feature.

### Pattern: all four match

Done. Volume, bbox, edges, and faces all agree to within tolerance — the parts are functionally identical for simple geometry.

## Reading the boolean diff (when reliable)

When `boolean_diff.reliable` is true and one or both diff STEPs were exported, opening them visually is the fastest debugging:

- **`diff_original_minus_draft.step` (missing material)**: shows the shape of what's missing. A cylinder shape → you didn't subtract a hole that should be there, or your subtractive feature is too small. A thin sliver → wrong fillet/chamfer size.
- **`diff_draft_minus_original.step` (extra material)**: shows what shouldn't be there. A cylinder shape → you have an extra subtractive feature in the wrong place, or you missed adding a hole. A thin sliver → fillet too small, or extra material outside the original envelope.

Both having geometry usually means **placement error** rather than feature mismatch — the same feature exists in both, but at different positions. Fix coordinates first.

## When boolean is unreliable

If `boolean_diff.reliable` is false, OCCT couldn't compute a meaningful diff (often because two separately-imported STEPs have microscopically different topology that breaks the boolean kernel). The fingerprint check is unaffected — it works by comparing geometric measurements directly, not by computing differences.

This is the normal case for many STEP comparisons. Don't treat unreliable booleans as evidence of a problem; just rely on the fingerprint.

## Iteration discipline

When the fingerprint reveals a mismatch, change only the relevant parameter or feature. Don't regenerate the whole script; that's what parametric code is for. If you set up dimensions as named constants at the top, most fixes are one-line changes followed by re-running verification.

If after **three iterations** the fingerprint isn't converging, stop and re-probe. You've likely misread the part's structure on the first pass — perhaps interpreted a fillet as a hole, missed a feature entirely, or got the symmetry wrong. Going back to step 1 with fresh eyes is faster than continuing to tweak.

A useful self-check before iteration 4: run `probe_step.py` on your latest draft and diff the JSON output against the original probe. If the structural counts (face types, hole groups, distinct radii) don't match, you have a structural error, not a dimension error.
