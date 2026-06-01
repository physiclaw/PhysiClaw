# Render pipeline

Photoreal Blender render of `tapz_20_solenoid_mount`, built from the
existing build123d assembly without modifying any procedure file.

```
hardware/assembly/render/
├── render.md              ← this file
├── materials_table.py     ← shared PBR table + HSV encode/decode
├── export_tapz_20.py      ← build the assembly → tag colors → write .glb
├── render_tapz_20.py      ← Blender script: import .glb → render .png
└── download_hdri.sh       ← fetch the studio HDRI from Poly Haven

hardware/output/render/    ← all generated artifacts land here
├── brown_photostudio_02_2k.hdr ← downloaded by download_hdri.sh   (~6 MB)
├── tapz_20_solenoid_mount.glb ← written by export_tapz_20.py (~11 MB)
└── tapz_20_solenoid_mount.png ← written by render_tapz_20.py
```

---

## Quickstart

```bash
# One-time: fetch the studio HDRI (idempotent, skips if already present)
hardware/assembly/render/download_hdri.sh

# Each time the build123d procedure or its parts change:
uv run --group cad python hardware/assembly/render/export_tapz_20.py

# Render (only needs the GLB + HDRI to be present):
/Applications/Blender.app/Contents/MacOS/Blender -b \
    -P hardware/assembly/render/render_tapz_20.py
```

Result: `hardware/output/render/tapz_20_solenoid_mount.png` (1920×1440
by default — bump the constants at the top of `render_tapz_20.py` for
higher-res hero shots).

---

## What each piece does

### `materials_table.py` — the contract between exporter and renderer

build123d's glTF writer in 0.10.0 preserves `.color` but DROPS `.label`
(an OCCT/XCAF quirk). To get materials across the export boundary, we
encode each material name as an HSV-spaced sRGB triple on the build123d
side, then decode it back to real PBR params in Blender.

`MATERIAL_LIST` is an ordered list of `(name, params)` pairs. The
index in the list IS the encoding — never reorder existing entries.
Adding a new material is fine, but every appearance shifts the hue
spacing (`hue = idx / N`), which means **any GLB tagged with the old
N must be re-exported**. The exporter and renderer must always agree
on the same table version.

Per-material params:
* `base` — linear-RGB tuple `(r, g, b)`.
* `metallic`, `roughness` — `[0..1]`. Metals = 1.0, dielectrics = 0.0.
* (optional) `transmission`, `ior` — for glass/clear plastic.

Self-test: `python materials_table.py` prints round-trip status.

### `export_tapz_20.py` — color-tag every leaf, then `export_gltf`

Builds the `TZ20SolenoidMount` compound (un-exploded), walks the tree,
and for every node whose label matches `LABEL_RULES` paints
`node.color = Color(*encode_color(material_name))`. XCAF then
propagates that color to sub-shapes inside the labelled compound —
this is how unlabelled MGN9H rail/slider children inherit
`Steel_Chrome` from their `MGN9H` parent. Anything not matched falls
through to `DEFAULT_MATERIAL` (`Steel_Chrome`).

`LABEL_RULES` is a list of `(predicate, material_name)` pairs;
predicates use `_starts(...)` / `_equals(...)` helpers. First match
wins. Edit this list to remap parts to different materials.

Output goes to `hardware/output/render/tapz_20_solenoid_mount.glb`.
The script prints a histogram of how many leaves got each material —
useful sanity check after any remapping.

### `render_tapz_20.py` — Blender 5.x driver

Top-of-file constants:

| Constant | Purpose |
|---|---|
| `RES_X`, `RES_Y` | Render resolution. 1920×1440 for preview, 3840×2880 for hero. |
| `SAMPLES` | Cycles samples. 256 is fine with denoising; bump for hero shots. |
| `SCENE_SCALE` | 1000.0 — GLB authors mm as meters; this displays it back at mm-scale. |
| `BEVEL_RADIUS` | 0.0002 m = 0.2 mm. Per-pixel edge rounding for the Bevel shader. |
| `BEVEL_MATERIALS` | Set of material names that get the Bevel-normal injection. Steel_Chrome and Aluminum_Polished are intentionally excluded — bevel softens the sharp edges where mirror-polish glints hardest. |
| `ROUGHNESS_VARY_MATERIALS` | Set of material names that get Noise→ColorRamp→Roughness microvariation, ±35 % around the table's roughness. Stops polished metal from reading as polished plastic. |

Steps `main()` runs, in order:
1. `_reset_scene()` — factory settings, fresh World.
2. `_import_glb(GLB_PATH)` — gltf importer.
3. `_rescale_roots(1000)` — GLB is in meters; ×1000 displays it at mm.
4. `_orient_for_ground()` — rotate the assembly so the frame's long
   axis lies horizontal and the bumper feet drop to z=0 (real-life pose
   for a benchtop tap-z machine).
5. `_upgrade_materials()` — for each imported material, decode the
   HSV-tag color via `materials_table.decode_material`, swap in the
   real PBR params, optionally inject the Bevel and Roughness-noise
   sub-graphs.
6. `_smooth_shade()` — `poly.use_smooth = True` for every mesh face.
7. `_build_world_hdri()` — white-limbo world: the camera ray sees a flat
   white tuned (`WHITE_BG_STRENGTH`) to match the lit floor, while
   reflection/diffuse rays see the desaturated studio HDRI
   (`WORLD_STRENGTH`) for neutral softbox glints on the metals. Split via
   Light-Path "Is Camera Ray". Errors with instructions if the HDRI is
   missing.
8. `_place_camera_and_lights()` — large flat white floor (grounding
   contact shadow) + a soft AREA rig (key, broad fill, and **two** back
   rims) + 55 mm-lens camera at az=30°, el=35° (industrial-product hero
   view; see notes below). The rig leans bright with a 2.5:1 key:fill and
   dual rims because the subject is mostly dark anodized/printed parts:
   dark products absorb light and collapse into one black mass, so they
   need more fill (shadow-side detail) and edge/rim light (form + contour)
   than a light-colored product would.
9. `_configure_cycles()` — engine=CYCLES, samples, denoise on, **AgX**
   Medium High Contrast (camera-like highlight rolloff; the Blender 4.0+
   default that replaced Filmic), exposure lifted to push the lit floor
   and limbo to white, GPU best-effort (METAL/OPTIX/CUDA/HIP/ONEAPI, CPU
   fallback).
10. `bpy.ops.render.render(write_still=True)` — output to `PNG_PATH`.

### `download_hdri.sh` — one-time HDRI fetch

Downloads `brown_photostudio_02_2k.hdr` from Poly Haven (CC0) into
`hardware/output/render/`. Idempotent: re-running is a no-op when the
full file is already present. Refetches if the local copy is truncated.
To swap to a different HDRI, change the URL/filename in the script and
the matching `HDRI_PATH` in `render_tapz_20.py`.

The studio HDRI matters because metals get their visual identity from
reflecting bright softboxes vs the dark gaps between them — a flat
gradient would make every metal collapse to the same shade. The HDRI is
desaturated before use (so its warm tint doesn't multiply into the
metals' F0) and the camera never sees it: the white-limbo world covers
the camera ray, so the HDRI only drives reflections and ambient.

---

## Tuning

### Visible horizon line between floor and backdrop
The floor and the white-limbo world must read the same value or a seam
appears at the floor's horizon. Tune `WHITE_BG_STRENGTH` in
`render_tapz_20.py`: raise it if the backdrop is darker than the floor,
lower it if brighter. It interacts with `_configure_cycles`' `exposure`
and `WORLD_STRENGTH` (ambient on the floor), so retune after changing
either. ~2.0 is the match for the default rig.

### Backdrop / metals too warm or too cool
Metals reflect the HDRI, which is desaturated to neutral in
`_build_world_hdri`. To dial reflection contrast use `WORLD_STRENGTH`
(higher = punchier bright/dark softbox glints). The visible backdrop is
independent — it's the flat `WHITE_BG_STRENGTH` white, not the HDRI.

### Dark parts read as one black lump
Two levers, both needed. **Material**: don't crush the anodized/printed
base colors to near-black — `Aluminum_Anod_Black` sits at ~sRGB 55 and
`PA12_Black_MJF` at ~sRGB 90 with `rough_vary`, dark enough to read as
black yet light enough that the T-slot grooves and edges catch the
environment. **Lighting**: keep the fill up (≈2.5:1 key:fill) and both
rims on (`RimL`/`RimR`) — they trace the dark frame's edges so its form
and silhouette read. Deepen the parts by lowering the base color; if they
then crush, raise fill/rim rather than the base (keeps the black look).

### Materials look wrong
Edit `materials_table.py`. After any change to `base` / `metallic` /
`roughness` of an EXISTING entry: re-render only (no re-export). After
ADDING a new entry: re-export the GLB too — the HSV slots have shifted.

### Wrong part got wrong material
Edit `LABEL_RULES` in `export_tapz_20.py`. Re-export the GLB, then
re-render. The export script's histogram tells you how the new mapping
distributed the 221 leaf parts.

### Polished metal looks like plastic
Add the affected material name to `ROUGHNESS_VARY_MATERIALS` in
`render_tapz_20.py`. The noise-driven roughness breaks the uniform
mirror surface that the human eye reads as plastic.

### Edges too sharp / not sharp enough
Two knobs in `render_tapz_20.py`:
* `BEVEL_RADIUS` — increase for chunkier edge highlights, decrease for
  cleaner CAD-style sharpness.
* `BEVEL_MATERIALS` — add/remove material names to apply or skip the
  bevel for that material.

### Camera framing
`_place_camera_and_lights` constants:
* `AZIMUTH_DEG` (30) — rotate around the vertical axis. 45° = more
  rotated view, 0° = head-on, 90° = pure profile.
* `ELEVATION_DEG` (35) — height above the horizon. 50° looks down
  into the gantry like an inspection shot; 20° is closer to eye-level.
* `FOCAL_LEN_MM` (55) — 35–50 mm widens the frame, 85 mm compresses
  perspective for tighter hero shots.
* `FIT_FACTOR` (1.20) — distance multiplier so the bbox fits with
  padding. Drop to 1.05 to fill the frame harder.

### Slow renders
* Drop `RES_X`/`RES_Y` to 1280×960 and `SAMPLES` to 128 for
  iteration; bump back up for the final.
* Confirm the `[render] GPU enabled (backend=METAL)` line in the log
  — if it says `CPU` you're missing GPU acceleration.

---

## Known limits

* `MATERIAL_LIST` ordering is load-bearing — see the warning above.
* The exporter walks the build123d compound tree and sets `.color` on
  labelled nodes only. Unlabelled raw `Part` leaves inherit the color
  from their nearest labelled ancestor via XCAF; if a part isn't
  taking the color you expect, give it a label in the part file (or a
  parent Compound with one).
* The backdrop is a white limbo, not a modeled cyclorama. At the 3/4-down
  hero angle a vertical back wall projects above the top of the frame, so
  a curved sweep buys nothing — the flat floor fills the frame up to the
  horizon and the white-limbo world takes over above it. If you lower
  `ELEVATION_DEG` toward eye level the floor's horizon drops into frame;
  if a hard line appears there, retune `WHITE_BG_STRENGTH` to match the
  floor (see Tuning), or model a cyc sweep for that near-horizontal view.
* The output dir (`hardware/output/`) is `.gitignore`d, so the
  rendered PNG and downloaded HDRI never get committed. Run
  `download_hdri.sh` on each fresh checkout.
