"""2020 aluminum T-slot extrusion — faithful 2020 cross-section.

Outer 20×20 with R1.5 corners, four T-slots (6.2 mm mouth, 1.8 mm
wall, 11 mm cavity), centre 7.8×7.8 block linked to the outer ring
by four diagonal ribs (1.5 mm thick), and a Ø5 axial bore. Padded
once to length. The 0.5 mm retention lip and cavity-back chamfers
in the published spec are dropped.

Cross-section dims live in `spec.py` (shared with the build123d
backend) so the two profiles can't drift. Only `Length` and
`BoreDiameter` are exposed via VarSet.

Build order matters in PartDesign: outer pad → fillet 4 vertical
corners (only 4 vertical edges exist at this point) → 4 T-slot
pockets → 4 rib pads → bore pocket. Ribs are extended slightly at
both ends so they overlap with the centre block and the corner
pads — bare endpoint contact isn't enough for PartDesign fusion.
"""

import math

from parts import StandardPart
from parts._fc import App, Part
from parts._helpers import (
    add_circle,
    attach_sketch_to_plane,
    find_vertical_edges,
    make_body,
    make_pad,
    make_pocket,
    make_varset,
)
from parts.extrusion_2020.spec import (
    BORDER_TO_CENTER_MM,
    EXTRUSION_2020_L300,
    FILLET_RADIUS_MM,
    OUTER_SIZE_MM,
    RIB_THICKNESS_MM,
    SLOT_CAVITY_MM,
    SLOT_MOUTH_DEPTH_MM,
    SLOT_MOUTH_MM,
)


_HALF = OUTER_SIZE_MM / 2
_CENTRE_HALF = _HALF - BORDER_TO_CENTER_MM
_RIB_OVERLAP = 0.5  # extend ribs by this much at each end to fuse cleanly


def _rotate(pt, angle_deg):
    a = math.radians(angle_deg)
    x, y = pt
    return (x * math.cos(a) - y * math.sin(a), x * math.sin(a) + y * math.cos(a))


# T-slot polygon — mouth opens to +Y, centred on x=0. Rotated to each face.
_T_SLOT_PTS = [
    (-SLOT_MOUTH_MM / 2,  _HALF),
    ( SLOT_MOUTH_MM / 2,  _HALF),
    ( SLOT_MOUTH_MM / 2,  _HALF - SLOT_MOUTH_DEPTH_MM),
    ( SLOT_CAVITY_MM / 2, _HALF - SLOT_MOUTH_DEPTH_MM),
    ( SLOT_CAVITY_MM / 2, _CENTRE_HALF),
    (-SLOT_CAVITY_MM / 2, _CENTRE_HALF),
    (-SLOT_CAVITY_MM / 2, _HALF - SLOT_MOUTH_DEPTH_MM),
    (-SLOT_MOUTH_MM / 2,  _HALF - SLOT_MOUTH_DEPTH_MM),
]


def _rib_corners(angle_deg):
    """Four corners of the diagonal rib rectangle at `angle_deg` (45/135/...).

    Centred at the midpoint between the centre-block corner and the outer
    corner along that diagonal. Length is extended by `_RIB_OVERLAP` at
    each end so the rib clearly intersects the centre block (inner end)
    and the rounded corner pad (outer end), forcing PartDesign to fuse.
    """
    base_len = (_HALF - _CENTRE_HALF) * math.sqrt(2)
    rib_len = base_len + 2 * _RIB_OVERLAP
    rib_centre_dist = (_CENTRE_HALF + _HALF) / 2
    a = math.radians(angle_deg)
    cx = rib_centre_dist * math.cos(a)
    cy = rib_centre_dist * math.sin(a)
    half_len = rib_len / 2
    half_thk = RIB_THICKNESS_MM / 2
    local = [
        (-half_len, -half_thk),
        ( half_len, -half_thk),
        ( half_len,  half_thk),
        (-half_len,  half_thk),
    ]
    return [
        (cx + lx * math.cos(a) - ly * math.sin(a),
         cy + lx * math.sin(a) + ly * math.cos(a))
        for lx, ly in local
    ]


def _add_polyline(sketch, pts):
    """Add `pts` as a closed polyline of LineSegments. Geometry only —
    cross-section dims are baked into the coordinates, no constraints."""
    n = len(pts)
    for i in range(n):
        p1 = App.Vector(pts[i][0], pts[i][1], 0)
        p2 = App.Vector(pts[(i + 1) % n][0], pts[(i + 1) % n][1], 0)
        sketch.addGeometry(Part.LineSegment(p1, p2), False)


class Extrusion2020(StandardPart):
    output_name = "Extrusion_2020_L300"

    def build(self):
        doc = App.newDocument(self.output_name)

        spec = self.spec
        vs = make_varset(doc, [
            ("Length",       spec.length_mm,        "Length", "Axial length (mm)"),
            ("BoreDiameter", spec.bore_diameter_mm, "Length", "Centre through-bore Ø (mm)"),
        ])

        body = make_body(doc, "Body")

        # 1. Outer 20×20.
        sk_outer = attach_sketch_to_plane(doc, body, "XY", "Sketch_Outer")
        _add_polyline(sk_outer, [
            ( _HALF,  _HALF),
            (-_HALF,  _HALF),
            (-_HALF, -_HALF),
            ( _HALF, -_HALF),
        ])
        pad_outer = make_pad(body, sk_outer, "Pad_Outer", varset=vs, length_var="Length")
        doc.recompute()  # next step reads pad_outer.Shape for the 4 vertical edges

        # 2. Fillet outer 4 vertical corner edges before T-slots create more.
        edge_refs = find_vertical_edges(pad_outer.Shape)
        if len(edge_refs) != 4:
            raise RuntimeError(f"expected 4 vertical edges on outer pad, got {len(edge_refs)}")
        fillet_feat = body.newObject("PartDesign::Fillet", "Fillet_Corners")
        fillet_feat.Base = (pad_outer, edge_refs)
        fillet_feat.Radius = FILLET_RADIUS_MM
        doc.recompute()

        # 3. Four T-slots — pocket through_all.
        for i, angle in enumerate((0, 90, 180, 270)):
            sk = attach_sketch_to_plane(doc, body, "XY", f"Sketch_TSlot_{i}")
            _add_polyline(sk, [_rotate(p, angle) for p in _T_SLOT_PTS])
            make_pocket(body, sk, f"Pocket_TSlot_{i}", through_all=True, reversed_=True)

        # 4. Four diagonal ribs — additive pads, fuse via overlap into centre + corner pads.
        for i, angle in enumerate((45, 135, 225, 315)):
            sk = attach_sketch_to_plane(doc, body, "XY", f"Sketch_Rib_{i}")
            _add_polyline(sk, _rib_corners(angle))
            make_pad(body, sk, f"Pad_Rib_{i}", varset=vs, length_var="Length")

        # 5. Ø5 axial bore.
        sk_bore = attach_sketch_to_plane(doc, body, "XY", "Sketch_Bore")
        add_circle(sk_bore, varset=vs, diameter_var="BoreDiameter", name="BoreRadius")
        make_pocket(body, sk_bore, "Pocket_Bore", through_all=True, reversed_=True)
        doc.recompute()

        return doc, body


PART = Extrusion2020(EXTRUSION_2020_L300)
