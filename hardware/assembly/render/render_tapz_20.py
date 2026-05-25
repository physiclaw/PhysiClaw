"""Blender 5.x render driver for tapz_20_solenoid_mount.

Invoke in background mode:

    /Applications/Blender.app/Contents/MacOS/Blender -b -P \\
        hardware/assembly/render/render_tapz_20.py

See render.md for the full pipeline + tuning notes.
"""

import math
import os
import sys

import bpy
import mathutils

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from materials_table import decode_material  # noqa: E402

PROJECT_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
OUT_DIR   = os.path.join(PROJECT_ROOT, "hardware", "output", "render")
GLB_PATH  = os.path.join(OUT_DIR, "tapz_20_solenoid_mount.glb")
PNG_PATH  = os.path.join(OUT_DIR, "tapz_20_solenoid_mount.png")
HDRI_PATH = os.path.join(OUT_DIR, "brown_photostudio_02_2k.hdr")

RES_X, RES_Y = 1920, 1440
SAMPLES      = 256
SCENE_SCALE  = 1000.0   # GLB authored in mm; importer reads as meters.

# Mesh data stays in meters even after the ×1000 transform-scale, so
# shader-space distances (Bevel radius) are in meters too.
BEVEL_RADIUS = 0.0002    # 0.2 mm


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


def _roots():
    return [
        obj for obj in bpy.data.objects
        if obj.parent is None and obj.type in ("MESH", "EMPTY")
    ]


def _rescale_roots(factor: float):
    roots = _roots()
    for obj in roots:
        obj.scale = (factor, factor, factor)
    bpy.context.view_layer.update()
    print(f"[render] scaled {len(roots)} root objects by ×{factor:g}")


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


def _orient_for_ground():
    """Lay the assembly horizontal with the bumper feet at z=0.

    build123d's frame puts the long axis along +Z with the bumpers on
    the −Y face; the glTF importer's Y-up→Z-up step rotates the empty,
    so we compose our rotation on top instead of clobbering it. Try
    −90° about X first; if the result isn't horizontal-aspect, swap.
    """
    roots = _roots()

    def _apply_x_rotation(deg):
        rot = mathutils.Matrix.Rotation(math.radians(deg), 4, "X")
        for obj in roots:
            obj.matrix_world = rot @ obj.matrix_world
        bpy.context.view_layer.update()

    _apply_x_rotation(-90)
    mins, maxs = _scene_bbox()
    if (maxs.y - mins.y) <= (maxs.z - mins.z):
        # Wrong direction; rotate +180° about X to flip the long axis
        # back to horizontal. Single transform pass — no undo dance.
        _apply_x_rotation(180)
        mins, maxs = _scene_bbox()

    # Drop so the lowest point sits at z=0.
    drop = mins.z
    for obj in roots:
        obj.location.z -= drop
    bpy.context.view_layer.update()
    mins.z -= drop
    maxs.z -= drop
    print(
        f"[render] oriented: bbox x=[{mins.x:.0f},{maxs.x:.0f}] "
        f"y=[{mins.y:.0f},{maxs.y:.0f}] z=[{mins.z:.0f},{maxs.z:.0f}]"
    )
    return mins, maxs


def _add_bevel(nt, bsdf):
    bevel = nt.nodes.new("ShaderNodeBevel")
    bevel.inputs["Radius"].default_value = BEVEL_RADIUS
    nt.links.new(bevel.outputs["Normal"], bsdf.inputs["Normal"])


def _add_roughness_noise(nt, bsdf, base_r):
    """Drive Roughness through Noise→ColorRamp so a smooth metal
    doesn't read as polished plastic."""
    r_lo = max(0.0, base_r * 0.65)
    r_hi = min(1.0, base_r * 1.35)
    coord = nt.nodes.new("ShaderNodeTexCoord")
    mapn  = nt.nodes.new("ShaderNodeMapping")
    noise = nt.nodes.new("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value     = 220.0
    noise.inputs["Detail"].default_value    = 4.0
    noise.inputs["Roughness"].default_value = 0.55
    ramp  = nt.nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].position = 0.35
    ramp.color_ramp.elements[1].position = 0.65
    ramp.color_ramp.elements[0].color    = (r_lo, r_lo, r_lo, 1.0)
    ramp.color_ramp.elements[1].color    = (r_hi, r_hi, r_hi, 1.0)
    nt.links.new(coord.outputs["Object"], mapn.inputs["Vector"])
    nt.links.new(mapn.outputs["Vector"],  noise.inputs["Vector"])
    nt.links.new(noise.outputs["Fac"],    ramp.inputs["Fac"])
    nt.links.new(ramp.outputs["Color"],   bsdf.inputs["Roughness"])


def _upgrade_materials():
    upgraded = []
    for mat in bpy.data.materials:
        if mat.node_tree is None:
            continue
        nt   = mat.node_tree
        bsdf = nt.nodes.get("Principled BSDF")
        if bsdf is None:
            continue
        rgb = list(bsdf.inputs["Base Color"].default_value)[:3]
        name, params = decode_material(rgb)
        bsdf.inputs["Base Color"].default_value = (*params["base"], 1.0)
        bsdf.inputs["Metallic"].default_value   = params["metallic"]
        bsdf.inputs["Roughness"].default_value  = params["roughness"]
        if params.get("bevel"):
            _add_bevel(nt, bsdf)
        if params.get("rough_vary"):
            _add_roughness_noise(nt, bsdf, params["roughness"])
        mat.name = name
        upgraded.append(name)
    print(f"[render] upgraded {len(upgraded)} materials: {sorted(set(upgraded))}")


def _smooth_shade():
    for obj in bpy.data.objects:
        if obj.type == "MESH":
            for poly in obj.data.polygons:
                poly.use_smooth = True


def _build_world_sky():
    """HDRI environment so metals reflect a real studio softbox shape
    instead of a flat sky gradient (which collapses every metal to the
    same grey on screen)."""
    world = bpy.context.scene.world
    nt = world.node_tree
    for n in list(nt.nodes):
        nt.nodes.remove(n)

    if not os.path.exists(HDRI_PATH):
        raise SystemExit(
            f"HDRI not found at {HDRI_PATH}\n"
            f"Run hardware/assembly/render/download_hdri.sh to fetch it."
        )
    env = nt.nodes.new("ShaderNodeTexEnvironment")
    env.image = bpy.data.images.load(HDRI_PATH)

    bg = nt.nodes.new("ShaderNodeBackground")
    bg.inputs["Strength"].default_value = 1.8

    out = nt.nodes.new("ShaderNodeOutputWorld")
    nt.links.new(env.outputs["Color"], bg.inputs["Color"])
    nt.links.new(bg.outputs["Background"], out.inputs["Surface"])
    print("[render] World: HDRI brown_photostudio_02")


def _add_ground_plane(center, diag, ground_z):
    mesh = bpy.data.meshes.new("Ground")
    obj  = bpy.data.objects.new("Ground", mesh)
    bpy.context.scene.collection.objects.link(obj)
    size = diag * 6
    mesh.from_pydata(
        [(-size, -size, 0), (size, -size, 0), (size, size, 0), (-size, size, 0)],
        [],
        [(0, 1, 2, 3)],
    )
    mesh.update()
    obj.location = (center.x, center.y, ground_z)

    mat = bpy.data.materials.new("Ground_Mat")
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf is not None:
        bsdf.inputs["Base Color"].default_value = (0.55, 0.55, 0.56, 1.0)
        bsdf.inputs["Roughness"].default_value  = 0.85
        bsdf.inputs["Metallic"].default_value   = 0.0
    obj.data.materials.append(mat)


def _place_camera_and_lights(mins, maxs):
    center = (mins + maxs) * 0.5
    diag   = (maxs - mins).length
    print(f"[render] bbox center=({center.x:.0f},{center.y:.0f},{center.z:.0f}) diag={diag:.0f}")

    # Ground 0.1 mm below the lowest mesh point — avoids z-fighting
    # without a visible gap at this camera distance.
    _add_ground_plane(center, diag, mins.z - 0.1)

    # Industrial product hero — see render.md for the rationale.
    AZIMUTH_DEG   = 30
    ELEVATION_DEG = 35
    FOCAL_LEN_MM  = 55
    SENSOR_W_MM   = 36
    FIT_FACTOR    = 1.20

    cam_data = bpy.data.cameras.new("Camera")
    cam_data.lens = FOCAL_LEN_MM
    cam_data.sensor_width = SENSOR_W_MM
    cam = bpy.data.objects.new("Camera", cam_data)
    bpy.context.scene.collection.objects.link(cam)

    az = math.radians(AZIMUTH_DEG)
    el = math.radians(ELEVATION_DEG)
    direction = mathutils.Vector((
        math.sin(az) * math.cos(el),
        -math.cos(az) * math.cos(el),
        math.sin(el),
    )).normalized()
    half_fov = math.atan((SENSOR_W_MM / 2) / FOCAL_LEN_MM)
    cam_distance = (diag / 2) * FIT_FACTOR / math.tan(half_fov)

    cam.location = center + direction * cam_distance
    cam.rotation_euler = (center - cam.location).to_track_quat("-Z", "Y").to_euler()
    bpy.context.scene.camera = cam
    print(
        f"[render] camera lens={FOCAL_LEN_MM}mm "
        f"az={AZIMUTH_DEG}° el={ELEVATION_DEG}° dist={cam_distance:.0f}"
    )

    energy = diag * diag * 12.0
    light_specs = [
        ("Key",  (1.1, -1.5,  1.7), energy * 1.0,  diag * 0.6),
        ("Fill", (-1.4, -0.8, 0.7), energy * 0.30, diag * 0.6),
        ("Rim",  (0.0,  1.6,  1.2), energy * 0.35, diag * 0.5),
    ]
    for name, dir_vec, en, size in light_specs:
        l_data = bpy.data.lights.new(name=f"Light_{name}", type="AREA")
        l_data.energy = en
        l_data.size   = size
        l_obj = bpy.data.objects.new(f"Light_{name}", l_data)
        bpy.context.scene.collection.objects.link(l_obj)
        l_obj.location = center + mathutils.Vector(dir_vec).normalized() * (diag * 1.5)
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
    sc.render.engine    = "CYCLES"
    sc.cycles.samples   = SAMPLES
    sc.cycles.use_denoising = True
    sc.view_settings.view_transform = "Filmic"
    sc.view_settings.look           = "Medium Contrast"
    sc.view_settings.exposure       = 0.7    # +0.7 EV ≈ 1.6× brighter
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
    mins, maxs = _orient_for_ground()
    _upgrade_materials()
    _smooth_shade()
    _build_world_sky()
    _place_camera_and_lights(mins, maxs)
    _configure_cycles()
    print(f"[render] rendering → {PNG_PATH}  ({RES_X}×{RES_Y}, {SAMPLES} spp)")
    bpy.ops.render.render(write_still=True)
    try:
        size_kb = os.path.getsize(PNG_PATH) / 1024
    except FileNotFoundError:
        raise SystemExit("[render] expected PNG was not written")
    print(f"[render] wrote {PNG_PATH} ({size_kb:.0f} KB)")


if __name__ == "__main__":
    main()
