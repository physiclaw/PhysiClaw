"""PBR material table for the Blender render pipeline.

The build123d glTF writer in 0.10.0 preserves `.color` but drops
`.label` (OCCT/XCAF quirk), so materials are smuggled across the
export boundary by encoding each material's index as an HSV-spaced
sRGB triple — see render.md for details and tuning notes.

Per-entry params:
    base, metallic, roughness — Principled BSDF inputs.
    bevel        — inject Bevel-shader normal in Blender.
    rough_vary   — drive Roughness through a Noise→ColorRamp graph
                   so polished metal doesn't read as polished plastic.
    anisotropic, anisotropic_rotation — directional reflections; the
                   render driver also wires in a Tangent node (Radial X)
                   so the streaks follow each part's object-local X
                   axis. Use for ground/brushed metal like MGN9H rails.

Index order is load-bearing: encode_color(name) returns hue = idx / N,
so any new material MUST be appended (never reordered), and the GLB
re-exported afterwards so old tags don't collide with new bins.
"""

import colorsys

MATERIAL_LIST = [
    ("Aluminum_Anod_Black", {
        # Black anodized aluminum (extrusions + motor housings). Lifted
        # off pure black to a dark charcoal: a base this low (was 0.02 ≈
        # sRGB 39, near the "darker than any real material" floor) crushes
        # the T-slot profile and part forms into one unreadable black mass.
        # At ~sRGB 55 the grooves and edges catch the environment and read
        # as black anodized aluminium — dark, but no longer a crushed
        # silhouette. Uniform roughness (no rough_vary): the noise speckle
        # read as dirt/staining on the big flat extrusion faces; clean
        # anodizing wants an even satin sheen.
        "base": (0.040, 0.040, 0.044),
        "metallic": 1.00,
        "roughness": 0.45,
        "bevel": True,
    }),
    ("Steel_Chrome", {
        # Bright stainless rail finish. The HDRI is desaturated upstream
        # (see _build_world_sky), so the previous cream-tint risk is
        # gone — push the F0 back toward proper polished stainless so
        # the rails clearly contrast with the near-black extrusions.
        "base": (0.78, 0.80, 0.83),
        "metallic": 1.00,
        "roughness": 0.16,
        "rough_vary": True,
        "anisotropic": 0.65,
        # Principled BSDF rotates highlight elongation 90° vs the
        # Glossy BSDF; +0.25 aligns streaks ALONG the tangent.
        "anisotropic_rotation": 0.25,
    }),
    ("Steel_Zinc", {
        "base": (0.72, 0.70, 0.66),
        "metallic": 1.00,
        "roughness": 0.42,
        "bevel": True,
    }),
    ("Steel_Black_Coated", {
        "base": (0.075, 0.072, 0.070),
        "metallic": 1.0,
        "roughness": 0.55,
        "bevel": True,
    }),
    ("PA12_Black_MJF", {
        # Matte MJF nylon (brackets, mounts). Lifted to a clear dark grey
        # so the printed parts read as a distinct material with visible
        # form, not crushed silhouettes merging into the anodized frame.
        "base": (0.105, 0.105, 0.105),
        "metallic": 0.00,
        "roughness": 0.70,
    }),
    # Matte black rubber — belt and bumper share the same pure-black
    # diffuse with grazing-angle Fresnel as their only sheen. Reads as a
    # near-zero-reflectance contributor to the dark band that includes
    # the extrusions and motor housings, but distinguishably less
    # "metallic" because metallic=0 keeps the face flat.
    ("Rubber_Belt", {
        "base": (0.000, 0.000, 0.000),
        "metallic": 0.00,
        "roughness": 0.85,
    }),
    ("Rubber_Bumper", {
        "base": (0.000, 0.000, 0.000),
        "metallic": 0.00,
        "roughness": 0.85,
    }),
    ("Aluminum_Polished", {
        # Machined / turned aluminum on the GT2 pulleys + idler bodies +
        # washer rings. What killed the "metal" read and made it look
        # plastic was a too-high roughness flattening the reflection into a
        # uniform grey — metal needs DISTINCT reflections. So: high base
        # (0.66, bright reflectance), roughness down to 0.40 so the HDRI
        # softboxes actually reflect, and anisotropy so they reflect as a
        # brushed/turned streak (lathe finish) rather than a smooth mirror —
        # the streak reads unmistakably as metal without going glossy.
        # `_add_anisotropy` wires a Radial-X tangent, so the streaks run
        # circumferentially around these turned parts.
        "base": (0.66, 0.66, 0.66),
        "metallic": 1.00,
        "roughness": 0.40,
        "anisotropic": 0.6,
        "anisotropic_rotation": 0.25,
    }),
]

_NAME_TO_INDEX = {name: idx for idx, (name, _) in enumerate(MATERIAL_LIST)}


def encode_color(name: str) -> tuple[float, float, float]:
    """sRGB tuple with an HSV-unique hue per material index."""
    return colorsys.hsv_to_rgb(_NAME_TO_INDEX[name] / len(MATERIAL_LIST), 0.9, 0.9)


def decode_material(rgb) -> tuple[str, dict]:
    """Reverse of encode_color — (name, params) from an sRGB triple."""
    n = len(MATERIAL_LIST)
    h, _, _ = colorsys.rgb_to_hsv(*rgb[:3])
    return MATERIAL_LIST[round(h * n) % n]


def _self_test() -> None:
    for name, _ in MATERIAL_LIST:
        rgb = encode_color(name)
        back, _ = decode_material(rgb)
        assert back == name, f"round-trip failed: {name} → {back}"
    print(f"materials_table: {len(MATERIAL_LIST)} entries, encode→decode round-trip OK")


if __name__ == "__main__":
    _self_test()
