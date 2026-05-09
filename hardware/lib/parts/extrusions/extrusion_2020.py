"""2020 aluminum T-slot extrusion — simplified profile, parametric on length.

Real 2020 has rounded corners and a more nuanced slot geometry; this
captures the load-bearing envelope: 20×20 outer square, Ø5 axial bore,
four T-slots (one per face) replicated by polar pattern.

Slot geometry is hardcoded since users almost never re-parameterize it
within a fixed 2020 profile — they swap to a different `extrusion_*`
file instead.
"""

import FreeCAD as App
import Part
from Sketcher import Constraint

from parts._helpers import (
    add_centered_rect,
    add_circle,
    attach_sketch_to_plane,
    make_body,
    make_pad,
    make_pocket,
    make_varset,
    origin_axis,
)


def _add_t_slot_profile(sketch):
    """Closed T-shaped polyline for the +X-face slot (8 vertices, 8 edges).

    Coordinates traverse the slot clockwise from the +X opening's top
    corner, dive into the body to the inner cavity, and return. Numbers
    are in mm; the slot opens at x=10 (the +X face of a centered 20×20
    square) and reaches inward toward the axis.
    """
    pts = [
        App.Vector(10.0,  3.0, 0),
        App.Vector( 8.2,  3.0, 0),
        App.Vector( 8.2,  5.5, 0),
        App.Vector( 3.6,  5.5, 0),
        App.Vector( 3.6, -5.5, 0),
        App.Vector( 8.2, -5.5, 0),
        App.Vector( 8.2, -3.0, 0),
        App.Vector(10.0, -3.0, 0),
    ]
    edges = []
    n = len(pts)
    for i in range(n):
        g = sketch.addGeometry(Part.LineSegment(pts[i], pts[(i + 1) % n]), False)
        edges.append(g)
    for i in range(n):
        sketch.addConstraint(Constraint("Coincident", edges[i], 2, edges[(i + 1) % n], 1))
    # Pin every vertex with absolute X/Y constraints — keeps the slot rigid
    # against accidental drag in the GUI.
    for i, p in enumerate(pts):
        sketch.addConstraint(Constraint("DistanceX", -1, 1, edges[i], 1, p.x))
        sketch.addConstraint(Constraint("DistanceY", -1, 1, edges[i], 1, p.y))
    return edges


def build():
    doc = App.newDocument("Extrusion_2020")

    vs = make_varset(doc, [
        ("Length",         300.0,  "Length",  "Axial length (mm)"),
        ("OuterSize",       20.0,  "Length",  "Outer cross-section square (mm)"),
        ("BoreDiameter",     5.0,  "Length",  "Centre through-bore Ø (mm)"),
    ])

    body = make_body(doc, "Body")

    sk_sq = attach_sketch_to_plane(doc, body, "XY", "Sketch_Profile")
    add_centered_rect(sk_sq, varset=vs, width_var="OuterSize", height_var="OuterSize")
    make_pad(body, sk_sq, "Pad_Profile", varset=vs, length_var="Length")

    sk_bore = attach_sketch_to_plane(doc, body, "XY", "Sketch_Bore")
    add_circle(sk_bore, varset=vs, diameter_var="BoreDiameter", name="BoreRadius")
    make_pocket(body, sk_bore, "Pocket_Bore", through_all=True, reversed_=True)

    sk_slot = attach_sketch_to_plane(doc, body, "XY", "Sketch_Slot")
    _add_t_slot_profile(sk_slot)
    pocket_slot = make_pocket(
        body, sk_slot, "Pocket_Slot", through_all=True, reversed_=True
    )

    pattern = body.newObject("PartDesign::PolarPattern", "Pattern_Slots")
    pattern.Originals = [pocket_slot]
    pattern.Axis = (origin_axis(body, "Z"), [""])
    pattern.Angle = 360.0
    pattern.Occurrences = 4
    # body.newObject() doesn't auto-promote the Tip — STEP exports would
    # otherwise dump the pre-pattern shape.
    body.Tip = pattern
    doc.recompute()

    return doc, body
