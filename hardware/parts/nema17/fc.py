"""NEMA 17 stepper motor — body, filleted corners, mount holes, boss, shaft."""

from parts import StandardPart
from parts._fc import App
from parts._helpers import (
    add_4_hole_pattern,
    add_centered_rect,
    add_circle,
    attach_sketch_to_face,
    attach_sketch_to_plane,
    find_face_by_position,
    find_vertical_edges,
    make_body,
    make_pad,
    make_pocket,
    make_varset,
)
from parts.nema17.spec import NEMA17_SPEC


class NEMA17(StandardPart):
    output_name = "NEMA17"

    def build(self):
        doc = App.newDocument(self.output_name)

        spec = self.spec
        vs = make_varset(doc, [
            ("BodySize",      spec.body_size_mm,      "Length",  "Body edge length (mm)"),
            ("BodyLength",    spec.body_length_mm,    "Length",  "Body axial length (mm)"),
            ("CornerRadius",  spec.corner_radius_mm,  "Length",  "Body corner radius (mm)"),
            ("BossDiameter",  spec.boss_diameter_mm,  "Length",  "Front pilot boss Ø (mm)"),
            ("BossHeight",    spec.boss_height_mm,    "Length",  "Front pilot boss height (mm)"),
            ("ShaftDiameter", spec.shaft_diameter_mm, "Length",  "Output shaft Ø (mm)"),
            ("ShaftLength",   spec.shaft_length_mm,   "Length",  "Shaft length above boss (mm)"),
            ("HoleSpacing",   spec.hole_spacing_mm,   "Length",  "Mounting-hole pattern (mm)"),
            ("HoleDiameter",  spec.hole_diameter_mm,  "Length",  "Mounting hole Ø (mm)"),
        ])

        body = make_body(doc, "Body")
        body_length = float(vs.BodyLength)

        sk_body = attach_sketch_to_plane(doc, body, "XY", "Sketch_Body")
        add_centered_rect(sk_body, varset=vs, width_var="BodySize", height_var="BodySize")
        pad_body = make_pad(body, sk_body, "Pad_Body", varset=vs, length_var="BodyLength")
        doc.recompute()  # next step reads pad_body.Shape for vertical edges

        edge_refs = find_vertical_edges(pad_body.Shape)
        if len(edge_refs) != 4:
            raise RuntimeError(f"expected 4 vertical edges, got {len(edge_refs)}")
        fillet = body.newObject("PartDesign::Fillet", "Fillet_Corners")
        fillet.Base = (pad_body, edge_refs)
        fillet.Radius = float(vs.CornerRadius)
        fillet.setExpression("Radius", f"<<{vs.Label}>>.CornerRadius")
        doc.recompute()  # next step reads fillet.Shape for the top face

        top_face = find_face_by_position(fillet.Shape, (0, 0, body_length))
        sk_holes = attach_sketch_to_face(doc, body, fillet, top_face, "Sketch_Holes")
        add_4_hole_pattern(sk_holes, vs, "HoleSpacing", "HoleDiameter")
        pocket_holes = make_pocket(body, sk_holes, "Pocket_Holes", through_all=True)
        doc.recompute()  # next step reads pocket_holes.Shape

        top_face_2 = find_face_by_position(pocket_holes.Shape, (0, 0, body_length))
        sk_boss = attach_sketch_to_face(doc, body, pocket_holes, top_face_2, "Sketch_Boss")
        add_circle(sk_boss, varset=vs, diameter_var="BossDiameter", name="BossRadius")
        pad_boss = make_pad(body, sk_boss, "Pad_Boss", varset=vs, length_var="BossHeight")
        doc.recompute()  # next step reads pad_boss.Shape

        boss_top_z = body_length + float(vs.BossHeight)
        boss_top = find_face_by_position(pad_boss.Shape, (0, 0, boss_top_z))
        sk_shaft = attach_sketch_to_face(doc, body, pad_boss, boss_top, "Sketch_Shaft")
        add_circle(sk_shaft, varset=vs, diameter_var="ShaftDiameter", name="ShaftRadius")
        make_pad(body, sk_shaft, "Pad_Shaft", varset=vs, length_var="ShaftLength")
        doc.recompute()

        return doc, body


PART = NEMA17(NEMA17_SPEC)
