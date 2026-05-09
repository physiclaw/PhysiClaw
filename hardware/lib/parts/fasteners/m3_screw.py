"""ISO 4762 socket-head cap screw, M3 — parametric on length."""

import math

import FreeCAD as App

from parts._helpers import (
    add_circle,
    add_regular_polygon,
    attach_sketch_to_face,
    attach_sketch_to_plane,
    find_face_by_position,
    make_body,
    make_pad,
    make_pocket,
    make_varset,
)


def build():
    doc = App.newDocument("M3_Screw")

    vs = make_varset(doc, [
        ("HeadDiameter",   5.5,  "Length",  "ISO 4762 M3 cap-head Ø (mm)"),
        ("HeadHeight",     3.0,  "Length",  "ISO 4762 M3 cap-head height (mm)"),
        ("ShaftDiameter",  3.0,  "Length",  "Nominal M3 shaft Ø (mm, no thread)"),
        ("ShaftLength",   10.0,  "Length",  "Under-head shank length (mm)"),
        ("HexAcrossFlats", 2.5,  "Length",  "Hex socket across-flats (mm)"),
        ("HexDepth",       2.0,  "Length",  "Hex socket depth (mm)"),
    ])

    body = make_body(doc, "Body")

    sk_head = attach_sketch_to_plane(doc, body, "XY", "Sketch_Head")
    add_circle(sk_head, varset=vs, diameter_var="HeadDiameter", name="HeadRadius")
    pad_head = make_pad(body, sk_head, "Pad_Head", varset=vs, length_var="HeadHeight")
    doc.recompute()  # next step reads pad_head.Shape

    head_bottom = find_face_by_position(pad_head.Shape, (0, 0, 0))
    sk_shaft = attach_sketch_to_face(doc, body, pad_head, head_bottom, "Sketch_Shaft")
    add_circle(sk_shaft, varset=vs, diameter_var="ShaftDiameter", name="ShaftRadius")
    make_pad(body, sk_shaft, "Pad_Shaft", varset=vs, length_var="ShaftLength")

    head_top_z = float(vs.HeadHeight)
    head_top = find_face_by_position(pad_head.Shape, (0, 0, head_top_z))
    sk_hex = attach_sketch_to_face(doc, body, pad_head, head_top, "Sketch_Hex")
    af = float(vs.HexAcrossFlats)
    add_regular_polygon(sk_hex, 6, af / math.sqrt(3), name_prefix="Hex")
    sk_hex.setExpression(
        "Constraints.HexRadius",
        f"<<{vs.Label}>>.HexAcrossFlats / sqrt(3)",
    )
    make_pocket(body, sk_hex, "Pocket_Hex", varset=vs, length_var="HexDepth")
    doc.recompute()

    return doc, body
