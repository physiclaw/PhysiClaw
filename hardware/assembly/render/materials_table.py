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

Index order is load-bearing: encode_color(name) returns hue = idx / N,
so any new material MUST be appended (never reordered), and the GLB
re-exported afterwards so old tags don't collide with new bins.
"""

import colorsys

MATERIAL_LIST = [
    ("Aluminum_Anod_Black", {
        "base": (0.045, 0.045, 0.050),
        "metallic": 1.00,
        "roughness": 0.35,
        "bevel": True,
    }),
    ("Steel_Chrome", {
        "base": (0.78, 0.80, 0.84),
        "metallic": 1.00,
        "roughness": 0.13,
        # Bevel intentionally OFF: it softens the rail flange's sharp
        # 90° corners, exactly where mirror-polish stainless glints
        # hardest.
        "rough_vary": True,
    }),
    ("Steel_Zinc", {
        "base": (0.72, 0.70, 0.66),
        "metallic": 1.00,
        "roughness": 0.42,
        "bevel": True,
    }),
    ("Steel_Black_Coated", {
        "base": (0.075, 0.072, 0.070),
        "metallic": 0.30,
        "roughness": 0.55,
        "bevel": True,
    }),
    ("PA12_Black_MJF", {
        "base": (0.060, 0.060, 0.060),
        "metallic": 0.00,
        "roughness": 0.70,
    }),
    ("Rubber_Belt", {
        "base": (0.030, 0.030, 0.030),
        "metallic": 0.00,
        "roughness": 0.85,
    }),
    ("Rubber_Bumper", {
        "base": (0.025, 0.025, 0.025),
        "metallic": 0.00,
        # Smooth molded face — catches a clean specular highlight
        # from the studio HDRI, per design ("the face is smooth").
        "roughness": 0.25,
    }),
    ("Aluminum_Polished", {
        "base": (0.78, 0.78, 0.78),
        "metallic": 1.00,
        "roughness": 0.42,
        # Same bevel reasoning as Steel_Chrome.
        "rough_vary": True,
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
