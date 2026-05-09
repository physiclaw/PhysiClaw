"""608ZZ deep-groove ball bearing — simplified as an annular cylinder.

Real 608ZZ has races, balls, and shields; for assembly modeling and
clearance checks the outer envelope is what matters.
"""

from parts._fc import App
from parts._helpers import (
    add_circle,
    attach_sketch_to_plane,
    make_body,
    make_pad,
    make_varset,
)


def build():
    doc = App.newDocument("Bearing_608")

    vs = make_varset(doc, [
        ("InnerDiameter",  8.0,  "Length",  "Bore Ø (mm)"),
        ("OuterDiameter", 22.0,  "Length",  "Outer race Ø (mm)"),
        ("Width",          7.0,  "Length",  "Axial width (mm)"),
    ])

    body = make_body(doc, "Body")

    # Two concentric circles in one sketch → Pad becomes an annulus.
    sk = attach_sketch_to_plane(doc, body, "XY", "Sketch_Profile")
    add_circle(sk, varset=vs, diameter_var="OuterDiameter", name="OuterRadius")
    add_circle(sk, varset=vs, diameter_var="InnerDiameter", name="InnerRadius")
    make_pad(body, sk, "Pad_Profile", varset=vs, length_var="Width")
    doc.recompute()

    return doc, body
