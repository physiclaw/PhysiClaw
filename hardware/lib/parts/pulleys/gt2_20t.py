"""GT2 timing pulley, 20 teeth, simplified circular tooth profile.

Real GT2 teeth are a curved pocket profile per the GT2 spec. For
assembly modelling and clearance checks a small circular cavity at
each tooth position is sufficient and keeps the headless build short.
"""

import FreeCAD as App
import Part
from Sketcher import Constraint

from parts._helpers import (
    add_circle,
    attach_sketch_to_plane,
    make_body,
    make_pad,
    make_pocket,
    make_varset,
    origin_axis,
)


def build():
    doc = App.newDocument("GT2_20T")

    vs = make_varset(doc, [
        ("Teeth",             20,    "Integer", "Tooth count"),
        ("Pitch",              2.0,  "Length",  "Belt pitch (mm)"),
        ("OuterDiameter",     12.2,  "Length",  "Outer Ø at tooth crests (mm)"),
        ("BeltWidth",          6.0,  "Length",  "Axial pulley height (mm)"),
        ("BoreDiameter",       5.0,  "Length",  "Shaft bore Ø (mm)"),
        ("ToothCavityRadius",  0.5,  "Length",  "Simplified tooth gap radius (mm)"),
    ])

    body = make_body(doc, "Body")

    sk_body = attach_sketch_to_plane(doc, body, "XY", "Sketch_Body")
    add_circle(sk_body, varset=vs, diameter_var="OuterDiameter", name="OuterRadius")
    make_pad(body, sk_body, "Pad_Body", varset=vs, length_var="BeltWidth")

    sk_bore = attach_sketch_to_plane(doc, body, "XY", "Sketch_Bore")
    add_circle(sk_bore, varset=vs, diameter_var="BoreDiameter", name="BoreRadius")
    make_pocket(body, sk_bore, "Pocket_Bore", through_all=True, reversed_=True)

    sk_tooth = attach_sketch_to_plane(doc, body, "XY", "Sketch_Tooth")
    tooth_x = float(vs.OuterDiameter) / 2.0
    tcr = float(vs.ToothCavityRadius)
    g = sk_tooth.addGeometry(
        Part.Circle(App.Vector(tooth_x, 0, 0), App.Vector(0, 0, 1), tcr), False
    )
    cdx = sk_tooth.addConstraint(Constraint("DistanceX", -1, 1, g, 3, tooth_x))
    sk_tooth.renameConstraint(cdx, "ToothX")
    cdy = sk_tooth.addConstraint(Constraint("DistanceY", -1, 1, g, 3, 0.0))
    sk_tooth.renameConstraint(cdy, "ToothY")
    cr = sk_tooth.addConstraint(Constraint("Radius", g, tcr))
    sk_tooth.renameConstraint(cr, "ToothRadius")
    sk_tooth.setExpression(
        "Constraints.ToothX", f"<<{vs.Label}>>.OuterDiameter / 2"
    )
    sk_tooth.setExpression(
        "Constraints.ToothRadius", f"<<{vs.Label}>>.ToothCavityRadius"
    )
    pocket_tooth = make_pocket(
        body, sk_tooth, "Pocket_Tooth", through_all=True, reversed_=True
    )

    pattern = body.newObject("PartDesign::PolarPattern", "Pattern_Teeth")
    pattern.Originals = [pocket_tooth]
    pattern.Axis = (origin_axis(body, "Z"), [""])
    pattern.Angle = 360.0
    pattern.Occurrences = int(vs.Teeth)
    pattern.setExpression("Occurrences", f"<<{vs.Label}>>.Teeth")
    body.Tip = pattern
    doc.recompute()

    return doc, body
