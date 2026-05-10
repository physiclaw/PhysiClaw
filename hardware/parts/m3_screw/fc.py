"""ISO 4762 socket-head cap screw, M3 — parametric on length."""

import math

from parts import StandardPart
from parts._fc import App
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
from parts.m3_screw.spec import M3_SCREW_X10


class M3Screw(StandardPart):
    output_name = "M3x10"

    def build(self):
        doc = App.newDocument(self.output_name)

        spec = self.spec
        vs = make_varset(doc, [
            ("HeadDiameter",   spec.head_diameter_mm,    "Length",  "ISO 4762 M3 cap-head Ø (mm)"),
            ("HeadHeight",     spec.head_height_mm,      "Length",  "ISO 4762 M3 cap-head height (mm)"),
            ("ShaftDiameter",  spec.shaft_diameter_mm,   "Length",  "Nominal M3 shaft Ø (mm, no thread)"),
            ("ShaftLength",    spec.shaft_length_mm,     "Length",  "Under-head shank length (mm)"),
            ("HexAcrossFlats", spec.hex_across_flats_mm, "Length",  "Hex socket across-flats (mm)"),
            ("HexDepth",       spec.hex_depth_mm,        "Length",  "Hex socket depth (mm)"),
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


PART = M3Screw(M3_SCREW_X10)
