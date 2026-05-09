"""2020 aluminum T-slot extrusion — single-sketch profile, parametric on length.

Cross-section is one closed sketch on XY: 20×20 outer with four R1.5
corner fillets, four T-slots (6.2 mm mouth, 1.8 mm wall, 11×4.6 mm
inner cavity), and a Ø5 axial bore. Padded once to length. The 0.5 mm
retention lip and the cavity-back chamfers in the published spec are
omitted — close enough for clearance and assembly modelling.

Cross-section dims are hardcoded since users almost never re-parameterize
them within a fixed 2020 — they swap to a different `extrusion_*` file.
Only `Length` and `BoreDiameter` are exposed via VarSet.
"""

import math

from parts._fc import App, Constraint, Part
from parts._helpers import (
    add_circle,
    attach_sketch_to_plane,
    make_body,
    make_pad,
    make_varset,
)


_OUTER = 10.0           # outer half-size (20 / 2)
_R_CORNER = 1.5         # corner fillet radius
_FACE_END = _OUTER - _R_CORNER     # 8.5 — face/arc tangent point
_SLOT_HALF = 3.1        # mouth half-height (6.2 / 2)
_CAVITY_HALF = 5.5      # cavity half-height (11 / 2)
_THROAT_X = 8.2         # cavity-front wall (10 - 1.8 wall thickness)
_CAVITY_BACK = 3.6      # cavity-back wall (clears the Ø5 bore by ~1 mm)


# Canonical +X face traversed CCW (i.e. in +Y direction), with the T-slot
# detour baked in: bottom corner endpoint → into the slot → top corner
# endpoint. Other 3 faces are 90° rotations of this list.
_FACE_PTS = [
    (_OUTER,        -_FACE_END),     # 0  bottom corner endpoint
    (_OUTER,        -_SLOT_HALF),    # 1  bottom of slot mouth
    (_THROAT_X,     -_SLOT_HALF),    # 2  throat floor inner end
    (_THROAT_X,     -_CAVITY_HALF),  # 3  cavity-front bottom
    (_CAVITY_BACK,  -_CAVITY_HALF),  # 4  cavity-back bottom
    (_CAVITY_BACK,   _CAVITY_HALF),  # 5  cavity-back top
    (_THROAT_X,      _CAVITY_HALF),  # 6  cavity-front top
    (_THROAT_X,      _SLOT_HALF),    # 7  throat ceiling inner end
    (_OUTER,         _SLOT_HALF),    # 8  top of slot mouth
    (_OUTER,         _FACE_END),     # 9  top corner endpoint
]


def _rot(p, k):
    """Rotate `(x, y)` by k * 90° CCW. Returns App.Vector."""
    x, y = p
    return [
        App.Vector( x,  y, 0),
        App.Vector(-y,  x, 0),
        App.Vector(-x, -y, 0),
        App.Vector( y, -x, 0),
    ][k]


def _add_outer_profile(sketch):
    """One closed CCW outline of the 20×20 cross-section: four faces (each
    9 line segments through the slot detour) joined by four R1.5 corner
    arcs. Pinned with absolute X/Y so the profile is rigid in the GUI."""
    face_pts = [[_rot(p, f) for p in _FACE_PTS] for f in range(4)]

    edges = []  # 10 per face: 9 lines, then 1 corner arc to next face
    for f in range(4):
        pts = face_pts[f]
        for i in range(9):
            edges.append(
                sketch.addGeometry(Part.LineSegment(pts[i], pts[i + 1]), False)
            )
        center = _rot((_FACE_END, _FACE_END), f)
        circle = Part.Circle(center, App.Vector(0, 0, 1), _R_CORNER)
        edges.append(sketch.addGeometry(
            Part.ArcOfCircle(circle, f * math.pi / 2, (f + 1) * math.pi / 2),
            False,
        ))

    n = len(edges)
    for i in range(n):
        sketch.addConstraint(
            Constraint("Coincident", edges[i], 2, edges[(i + 1) % n], 1)
        )

    # Pin endpoint 1 of every edge (10 face vertices) + each arc's radius.
    # Each arc is then fully constrained by its two endpoints + radius;
    # centre and end angles fall out without over-specification.
    for f in range(4):
        for i in range(10):
            edge_id = edges[f * 10 + i]
            p = face_pts[f][i]
            sketch.addConstraint(Constraint("DistanceX", -1, 1, edge_id, 1, p.x))
            sketch.addConstraint(Constraint("DistanceY", -1, 1, edge_id, 1, p.y))
        sketch.addConstraint(Constraint("Radius", edges[f * 10 + 9], _R_CORNER))


def build():
    doc = App.newDocument("Extrusion_2020")

    vs = make_varset(doc, [
        ("Length",       300.0, "Length", "Axial length (mm)"),
        ("BoreDiameter",   5.0, "Length", "Centre through-bore Ø (mm)"),
    ])

    body = make_body(doc, "Body")

    sketch = attach_sketch_to_plane(doc, body, "XY", "Sketch_Profile")
    _add_outer_profile(sketch)
    # Bore as an inner closed contour in the same sketch — pad treats it
    # as a hole automatically.
    add_circle(sketch, varset=vs, diameter_var="BoreDiameter", name="BoreRadius")

    make_pad(body, sketch, "Pad_Profile", varset=vs, length_var="Length")

    doc.recompute()
    return doc, body
