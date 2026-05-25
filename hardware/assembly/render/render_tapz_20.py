"""Phase-3 + Phase-4 Blender render driver for tapz_20_solenoid_mount.

Invoke with the Blender binary, in background mode:

    /Applications/Blender.app/Contents/MacOS/Blender -b -P \\
        hardware/assembly/render/render_tapz_20.py

Inputs:
    materials_table.py                            (alongside this file)
    hardware/output/render/tapz_20_solenoid_mount.glb
    hardware/output/render/studio_small_09_2k.hdr
        run hardware/assembly/render/download_hdri.sh to fetch it

Output:
    hardware/output/render/tapz_20_solenoid_mount.png
        1920×1440 Cycles + denoise (bump via RES_X/RES_Y/SAMPLES)

Phase-4 enhancements over the baseline:
    * Sky Texture (Nishita atmospheric model) drives the World — gives
      metals something real to reflect without needing an HDRI file.
    * Bevel-shader normal injection on the metallic materials → physical
      edge highlights without modifying geometry. Skipped on PA12 /
      rubber (matte, no edge sparkle expected).
    * Light grey ground plane catches contact shadows and grounds the
      assembly instead of floating in void.
    * Samples bumped to 256 + denoise on (Phase-4 quality bump).
"""

import math
import os
import sys

import bpy
import mathutils

# Materials table lives next to this script.
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from materials_table import decode_material  # noqa: E402

PROJECT_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
OUT_DIR   = os.path.join(PROJECT_ROOT, "hardware", "output", "render")
GLB_PATH  = os.path.join(OUT_DIR, "tapz_20_solenoid_mount.glb")
PNG_PATH  = os.path.join(OUT_DIR, "tapz_20_solenoid_mount.png")
HDRI_PATH = os.path.join(OUT_DIR, "studio_small_09_2k.hdr")

RES_X, RES_Y = 1920, 1440     # fast preview; bump to 3840×2880 for hero.
SAMPLES      = 256
SCENE_SCALE  = 1000.0   # GLB authored in mm; importer reads as meters.

# Bevel radius for the edge-highlight shader (Cycles only). The mesh
# data is in METERS (glTF spec), even though we transform-scale the
# root by ×1000 for display — the shader operates on mesh-local
# coordinates, so radius is also in meters. 0.2 mm → 0.0002 m gives
# a visible-but-subtle edge highlight on engineering parts.
BEVEL_RADIUS = 0.0002

# Materials that get the bevel-normal treatment (metals benefit; matte
# dielectrics do not). Steel_Chrome is intentionally EXCLUDED: the
# bevel shader softens the sharp 90° rail-flange corners, which is
# exactly where mirror-polish stainless should glint hardest. Same
# reason for the bright Aluminum_Polished pulleys/rings.
BEVEL_MATERIALS = {
    "Aluminum_Anod_Black",
    "Steel_Zinc",
    "Steel_Black_Coated",
}

# Materials that get Phase-4.3 roughness microvariation. Without this,
# smooth metal in PBR collapses to "polished plastic with grey
# pigment" — real machined / chrome-plated steel has visible
# grind-marks and micro-scratches that scatter the reflection.
ROUGHNESS_VARY_MATERIALS = {
    "Steel_Chrome",
    "Aluminum_Polished",
}


def _reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    if scene.world is None:
        scene.world = bpy.data.worlds.get("World") or bpy.data.worlds.new("World")
    scene.world.use_nodes = True


def _import_glb(path: str):
    if not os.path.exists(path):
        raise SystemExit(f"GLB not found: {path}\nRun export_tapz_20.py first.")
    bpy.ops.import_scene.gltf(filepath=path)
    print(f"[render] imported {path}")


def _rescale_roots(factor: float):
    n = 0
    for obj in bpy.data.objects:
        if obj.parent is None and obj.type in ("MESH", "EMPTY"):
            obj.scale = (factor, factor, factor)
            n += 1
    bpy.context.view_layer.update()
    print(f"[render] scaled {n} root objects by ×{factor:g}")


def _orient_for_ground():
    """Lay the assembly down so the bumpers (the frame's feet) rest on
    the ground plane, not its long edge.

    In build123d the frame's long axis is +Z and the bumpers screw onto
    the -Y face of the frame. The glTF importer applies a Y-up→Z-up
    conversion via the root empty's rotation, so we COMPOSE our own
    rotation on top of that (don't clobber the import rotation) to
    bring the bumper face to point straight down.

    We then choose the rotation by inspecting the post-rotation bbox:
    try −90° about X first; if the bumpers don't end up at the bottom,
    flip to +90°. This keeps the script robust to importer changes.
    """
    roots = [
        obj for obj in bpy.data.objects
        if obj.parent is None and obj.type in ("MESH", "EMPTY")
    ]

    # Try −90° around X first; check that the resulting world bbox is
    # WIDER (long axis horizontal) than it is TALL. If not, fall back
    # to +90°.
    def _apply_x_rotation(deg):
        rot = mathutils.Matrix.Rotation(math.radians(deg), 4, "X")
        for obj in roots:
            obj.matrix_world = rot @ obj.matrix_world
        bpy.context.view_layer.update()

    def _aspect_horizontal():
        mins, maxs = _scene_bbox()
        return (maxs.y - mins.y) > (maxs.z - mins.z)  # horizontal long axis

    _apply_x_rotation(-90)
    if not _aspect_horizontal():
        # Undo and try the other direction.
        _apply_x_rotation(+90)   # undo
        _apply_x_rotation(+90)   # apply the other way

    # Drop the whole thing so the lowest point touches z=0.
    mins, _ = _scene_bbox()
    drop = mins.z
    for obj in roots:
        obj.location.z -= drop
    bpy.context.view_layer.update()
    mins2, maxs2 = _scene_bbox()
    print(
        f"[render] oriented: bbox now "
        f"x=[{mins2.x:.0f},{maxs2.x:.0f}] "
        f"y=[{mins2.y:.0f},{maxs2.y:.0f}] "
        f"z=[{mins2.z:.0f},{maxs2.z:.0f}]  (bumpers at z≈0)"
    )


def _upgrade_materials():
    """Decode the HSV-tag color on every imported material, swap in the
    real PBR params, optionally inject a Bevel→Normal node for crisp
    edge highlights, and rename the material to the decoded name."""
    upgraded = []
    for mat in bpy.data.materials:
        if mat.node_tree is None:
            continue
        nt = mat.node_tree
        bsdf = nt.nodes.get("Principled BSDF")
        if bsdf is None:
            continue
        rgb = list(bsdf.inputs["Base Color"].default_value)[:3]
        name, params = decode_material(rgb)
        bsdf.inputs["Base Color"].default_value = (*params["base"], 1.0)
        bsdf.inputs["Metallic"].default_value   = params["metallic"]
        bsdf.inputs["Roughness"].default_value  = params["roughness"]
        # Optional transmission (4.x: "Transmission"; 4.4+/5.x: "Transmission Weight").
        if "transmission" in params:
            t_input = (bsdf.inputs.get("Transmission Weight")
                       or bsdf.inputs.get("Transmission"))
            if t_input is not None:
                t_input.default_value = params["transmission"]
                ior = bsdf.inputs.get("IOR")
                if ior is not None:
                    ior.default_value = params.get("ior", 1.45)
        # Bevel-shader normal for metals (Cycles only — no-op in Eevee).
        if name in BEVEL_MATERIALS:
            bevel = nt.nodes.new("ShaderNodeBevel")
            bevel.inputs["Radius"].default_value = BEVEL_RADIUS
            nt.links.new(bevel.outputs["Normal"], bsdf.inputs["Normal"])
        # Roughness microvariation (Phase 4.3) — Noise → ColorRamp →
        # Roughness, mapping noise 0..1 onto base_roughness ± 35%. Saves
        # the metals from looking like polished plastic.
        if name in ROUGHNESS_VARY_MATERIALS:
            base_r = params["roughness"]
            r_lo   = max(0.0, base_r - base_r * 0.35)
            r_hi   = min(1.0, base_r + base_r * 0.35)

            coord  = nt.nodes.new("ShaderNodeTexCoord")
            mapn   = nt.nodes.new("ShaderNodeMapping")
            # Bias and scale moot-most-anywhere; the scale puts the
            # noise frequency at "scratches" granularity in mesh space.
            noise  = nt.nodes.new("ShaderNodeTexNoise")
            noise.inputs["Scale"].default_value      = 220.0
            noise.inputs["Detail"].default_value     = 4.0
            noise.inputs["Roughness"].default_value  = 0.55
            ramp   = nt.nodes.new("ShaderNodeValToRGB")
            ramp.color_ramp.elements[0].position = 0.35
            ramp.color_ramp.elements[1].position = 0.65
            ramp.color_ramp.elements[0].color    = (r_lo, r_lo, r_lo, 1.0)
            ramp.color_ramp.elements[1].color    = (r_hi, r_hi, r_hi, 1.0)
            nt.links.new(coord.outputs["Object"], mapn.inputs["Vector"])
            nt.links.new(mapn.outputs["Vector"],  noise.inputs["Vector"])
            nt.links.new(noise.outputs["Fac"],    ramp.inputs["Fac"])
            nt.links.new(ramp.outputs["Color"],   bsdf.inputs["Roughness"])

        mat.name = name
        upgraded.append(name)
    print(f"[render] upgraded {len(upgraded)} materials: {sorted(set(upgraded))}")


def _smooth_shade():
    for obj in bpy.data.objects:
        if obj.type == "MESH":
            for poly in obj.data.polygons:
                poly.use_smooth = True


def _scene_bbox():
    mins = mathutils.Vector(( math.inf,  math.inf,  math.inf))
    maxs = mathutils.Vector((-math.inf, -math.inf, -math.inf))
    seen = False
    for obj in bpy.data.objects:
        if obj.type != "MESH":
            continue
        for corner in obj.bound_box:
            world = obj.matrix_world @ mathutils.Vector(corner)
            for i in range(3):
                if world[i] < mins[i]: mins[i] = world[i]
                if world[i] > maxs[i]: maxs[i] = world[i]
            seen = True
    if not seen:
        raise SystemExit("[render] no MESH objects after import")
    return mins, maxs


def _build_world_sky():
    """Swap the default flat World for an HDRI studio environment.

    Sky Texture produces only a smooth blue/grey gradient — every metal
    surface reflects the same blue dome, so different metals collapse
    to the same on-screen value. A real studio HDRI has rectangular
    soft-box highlights and dark gaps between them, which is what
    gives chrome / aluminum / steel their distinct visual signatures.
    """
    world = bpy.context.scene.world
    nt = world.node_tree
    for n in list(nt.nodes):
        nt.nodes.remove(n)

    env = nt.nodes.new("ShaderNodeTexEnvironment")
    if not os.path.exists(HDRI_PATH):
        raise SystemExit(
            f"HDRI not found at {HDRI_PATH}\n"
            f"Run hardware/assembly/render/download_hdri.sh to fetch it."
        )
    env.image = bpy.data.images.load(HDRI_PATH)

    bg = nt.nodes.new("ShaderNodeBackground")
    bg.inputs["Strength"].default_value = 1.8

    out = nt.nodes.new("ShaderNodeOutputWorld")
    nt.links.new(env.outputs["Color"], bg.inputs["Color"])
    nt.links.new(bg.outputs["Background"], out.inputs["Surface"])
    print("[render] World: HDRI studio_small_09")


def _add_ground_plane(center, diag):
    """Light-grey shadow-catching plane just under the lowest part."""
    mesh = bpy.data.meshes.new("Ground")
    obj  = bpy.data.objects.new("Ground", mesh)
    bpy.context.scene.collection.objects.link(obj)
    # Big square plane underneath the assembly's lowest Z. After
    # `_orient_for_ground` the bumpers sit at z=0, so the ground sits
    # just below them with a hair of clearance so it doesn't z-fight.
    mins, _ = _scene_bbox()
    size = diag * 6
    z = mins.z - 0.1     # ~0.1 mm below the lowest point
    bm_verts = [
        (-size, -size, 0),
        ( size, -size, 0),
        ( size,  size, 0),
        (-size,  size, 0),
    ]
    mesh.from_pydata(bm_verts, [], [(0, 1, 2, 3)])
    mesh.update()
    obj.location = (center.x, center.y, z)

    mat = bpy.data.materials.new("Ground_Mat")
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf is not None:
        bsdf.inputs["Base Color"].default_value = (0.55, 0.55, 0.56, 1.0)
        bsdf.inputs["Roughness"].default_value  = 0.85
        bsdf.inputs["Metallic"].default_value   = 0.0
    obj.data.materials.append(mat)
    for poly in obj.data.polygons:
        poly.use_smooth = False


def _place_camera_and_lights():
    mins, maxs = _scene_bbox()
    center = (mins + maxs) * 0.5
    diag   = (maxs - mins).length
    print(f"[render] bbox center=({center.x:.0f},{center.y:.0f},{center.z:.0f}) diag={diag:.0f}")

    _add_ground_plane(center, diag)

    # Hero / three-quarter view per industrial product-photography
    # conventions:
    #   * 55 mm-equivalent lens — slightly longer than the human-eye
    #     50 mm default, so straight edges stay straight (wide-angle
    #     distorts the long extrusions) but the scene doesn't compress
    #     telephoto-style.
    #   * 30° azimuth from the front-long-axis — classic 3/4 view that
    #     reveals two sides + the top of the gantry simultaneously.
    #   * 35° elevation — semi-aerial; looks DOWN into the frame so the
    #     X-rail, belt path, and solenoid mount inside the frame are
    #     all visible, not hidden behind the top extrusion.
    #   * Distance derived from the bbox diagonal + focal length so the
    #     full assembly fits with ≈ 20% padding regardless of how the
    #     scene is oriented or scaled.
    AZIMUTH_DEG   = 30
    ELEVATION_DEG = 35
    FOCAL_LEN_MM  = 55
    SENSOR_W_MM   = 36       # Blender's default full-frame sensor
    FIT_FACTOR    = 1.20     # padding around the bbox diagonal

    cam_data = bpy.data.cameras.new("Camera")
    cam_data.lens = FOCAL_LEN_MM
    cam_data.sensor_width = SENSOR_W_MM
    cam = bpy.data.objects.new("Camera", cam_data)
    bpy.context.scene.collection.objects.link(cam)

    az = math.radians(AZIMUTH_DEG)
    el = math.radians(ELEVATION_DEG)
    direction = mathutils.Vector((
        math.sin(az) * math.cos(el),     # +X (right of the long axis)
        -math.cos(az) * math.cos(el),    # -Y (front of the long axis)
        math.sin(el),                    # +Z (above)
    )).normalized()

    half_fov = math.atan((SENSOR_W_MM / 2) / FOCAL_LEN_MM)
    cam_distance = (diag / 2) * FIT_FACTOR / math.tan(half_fov)

    cam.location = center + direction * cam_distance
    look_dir = center - cam.location
    cam.rotation_euler = look_dir.to_track_quat("-Z", "Y").to_euler()
    bpy.context.scene.camera = cam
    print(
        f"[render] camera lens={FOCAL_LEN_MM}mm  "
        f"az={AZIMUTH_DEG}° el={ELEVATION_DEG}°  dist={cam_distance:.0f}  "
        f"pos=({cam.location.x:.0f},{cam.location.y:.0f},{cam.location.z:.0f})"
    )

    # Three-point AREA light rig — gentler with the Sky doing most of
    # the ambient work, but still want directional shaping.
    energy = diag * diag * 12.0
    light_specs = [
        ("Key",  (1.1, -1.5,  1.7), energy * 1.0,  diag * 0.6),
        ("Fill", (-1.4, -0.8, 0.7), energy * 0.30, diag * 0.6),
        ("Rim",  (0.0,  1.6,  1.2), energy * 0.35, diag * 0.5),
    ]
    for name, dir_vec, en, size in light_specs:
        l_data = bpy.data.lights.new(name=f"Light_{name}", type="AREA")
        l_data.energy = en
        l_data.size = size
        l_obj = bpy.data.objects.new(f"Light_{name}", l_data)
        bpy.context.scene.collection.objects.link(l_obj)
        offset = mathutils.Vector(dir_vec).normalized() * (diag * 1.5)
        l_obj.location = center + offset
        l_obj.rotation_euler = (center - l_obj.location).to_track_quat("-Z", "Y").to_euler()


def _try_enable_gpu():
    prefs = bpy.context.preferences.addons["cycles"].preferences
    for backend in ("METAL", "OPTIX", "CUDA", "HIP", "ONEAPI"):
        try:
            prefs.compute_device_type = backend
            prefs.get_devices()
            for d in prefs.devices:
                d.use = True
            if any(d.use for d in prefs.devices):
                bpy.context.scene.cycles.device = "GPU"
                print(f"[render] GPU enabled (backend={backend})")
                return
        except Exception:
            continue
    bpy.context.scene.cycles.device = "CPU"
    print("[render] GPU unavailable; rendering on CPU")


def _configure_cycles():
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    sc.cycles.samples = SAMPLES
    sc.cycles.use_denoising = True
    sc.view_settings.view_transform = "Filmic"
    sc.view_settings.look = "Medium Contrast"
    sc.render.resolution_x = RES_X
    sc.render.resolution_y = RES_Y
    sc.render.resolution_percentage = 100
    sc.render.image_settings.file_format = "PNG"
    os.makedirs(os.path.dirname(PNG_PATH), exist_ok=True)
    sc.render.filepath = PNG_PATH
    _try_enable_gpu()


def main():
    _reset_scene()
    _import_glb(GLB_PATH)
    _rescale_roots(SCENE_SCALE)
    _orient_for_ground()
    _upgrade_materials()
    _smooth_shade()
    _build_world_sky()
    _place_camera_and_lights()
    _configure_cycles()
    print(f"[render] rendering → {PNG_PATH}  ({RES_X}×{RES_Y}, {SAMPLES} spp)")
    bpy.ops.render.render(write_still=True)
    if not os.path.exists(PNG_PATH):
        raise SystemExit("[render] expected PNG was not written")
    size_kb = os.path.getsize(PNG_PATH) / 1024
    print(f"[render] wrote {PNG_PATH} ({size_kb:.0f} KB)")


if __name__ == "__main__":
    main()
