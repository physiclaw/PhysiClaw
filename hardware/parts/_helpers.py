"""
FreeCAD Part Design + VarSet helpers shared across part scripts.

Targets FreeCAD 1.0+ (VarSet, AttachmentSupport). Headless-safe — no
FreeCADGui calls. Expressions use the `<<VarSet>>.Property` form, not
the legacy `Spreadsheet.alias` form.
"""

import math

from parts._fc import App, Constraint, Part

# ─── VarSet ──────────────────────────────────────────────────────────

# PropertyLength preserves mm units in expressions; PropertyFloat does not.
_PROP_TYPE_MAP = {
    "Length": "App::PropertyLength",
    "Distance": "App::PropertyDistance",
    "Angle": "App::PropertyAngle",
    "Integer": "App::PropertyInteger",
    "Float": "App::PropertyFloat",
    "Bool": "App::PropertyBool",
}

_AXIS_ROLES = {"X": "X_Axis", "Y": "Y_Axis", "Z": "Z_Axis"}
_PLANE_ROLES = {"XY": "XY_Plane", "XZ": "XZ_Plane", "YZ": "YZ_Plane"}


def make_varset(doc, params, name="Parameters"):
    """Create an `App::VarSet` named `name` with the given typed properties.

    `params` is a sequence of `(prop_name, value, type_key, doc)` tuples
    where `type_key` is one of the keys in `_PROP_TYPE_MAP`.
    """
    vs = doc.addObject("App::VarSet", name)
    vs.Label = name
    for prop_name, value, type_key, description in params:
        try:
            fc_type = _PROP_TYPE_MAP[type_key]
        except KeyError as e:
            raise ValueError(
                f"Unknown VarSet type {type_key!r} for {prop_name!r}; "
                f"valid: {sorted(_PROP_TYPE_MAP)}"
            ) from e
        vs.addProperty(fc_type, prop_name, "Parameters", description)
        setattr(vs, prop_name, value)
    return vs


# ─── Body + Origin ────────────────────────────────────────────────────


def make_body(doc, name="Body"):
    """Create a PartDesign Body. Forces a recompute so `body.Origin` is populated."""
    body = doc.addObject("PartDesign::Body", name)
    doc.recompute()
    _ = body.Origin.OriginFeatures  # materialise the 6 base features
    return body


def _origin_feature(body, role):
    for feature in body.Origin.OriginFeatures:
        if feature.Role == role:
            return feature
    raise RuntimeError(f"origin feature with Role={role!r} not found on body {body.Label}")


def origin_axis(body, axis):
    """Return the body's X/Y/Z origin-axis feature (used for PolarPattern, etc.)."""
    if axis not in _AXIS_ROLES:
        raise ValueError(f"axis must be one of X/Y/Z, got {axis!r}")
    return _origin_feature(body, _AXIS_ROLES[axis])


def attach_sketch_to_plane(doc, body, plane, name):
    """Attach a sketch to one of the body's base planes (XY/XZ/YZ)."""
    if plane not in _PLANE_ROLES:
        raise ValueError(f"plane must be one of XY/XZ/YZ, got {plane!r}")
    support = _origin_feature(body, _PLANE_ROLES[plane])
    sketch = body.newObject("Sketcher::SketchObject", name)
    sketch.AttachmentSupport = [(support, "")]
    sketch.MapMode = "FlatFace"
    return sketch


def attach_sketch_to_face(doc, body, support_feature, face_ref, name):
    """Attach a sketch to a named face (e.g. 'Face3') of an existing feature."""
    sketch = body.newObject("Sketcher::SketchObject", name)
    sketch.AttachmentSupport = [(support_feature, [face_ref])]
    sketch.MapMode = "FlatFace"
    return sketch


# ─── Sketch geometry primitives ──────────────────────────────────────


def _bind_expression(target, prop_path, varset, expr):
    """Bind `target.<prop_path>` to a VarSet expression. `expr` is the
    text after `<<VarSet>>.`, e.g. `"HeadDiameter / 2"`."""
    if varset is None or expr is None:
        return
    target.setExpression(prop_path, f"<<{varset.Label}>>.{expr}")


def add_centered_rect(sketch, varset=None, width_var=None, height_var=None,
                      width=None, height=None):
    """Centered axis-aligned rectangle around the sketch origin.

    If `varset` + `width_var`/`height_var` are provided, the Width/Height
    constraints are bound to those VarSet properties. Otherwise the
    literal `width`/`height` are used.
    """
    if width is None:
        width = float(getattr(varset, width_var))
    if height is None:
        height = float(getattr(varset, height_var))
    hw, hh = width / 2.0, height / 2.0
    p1 = App.Vector(-hw, -hh, 0)
    p2 = App.Vector(hw, -hh, 0)
    p3 = App.Vector(hw, hh, 0)
    p4 = App.Vector(-hw, hh, 0)
    g_b = sketch.addGeometry(Part.LineSegment(p1, p2), False)
    g_r = sketch.addGeometry(Part.LineSegment(p2, p3), False)
    g_t = sketch.addGeometry(Part.LineSegment(p3, p4), False)
    g_l = sketch.addGeometry(Part.LineSegment(p4, p1), False)
    sketch.addConstraint(Constraint("Coincident", g_b, 2, g_r, 1))
    sketch.addConstraint(Constraint("Coincident", g_r, 2, g_t, 1))
    sketch.addConstraint(Constraint("Coincident", g_t, 2, g_l, 1))
    sketch.addConstraint(Constraint("Coincident", g_l, 2, g_b, 1))
    sketch.addConstraint(Constraint("Horizontal", g_b))
    sketch.addConstraint(Constraint("Horizontal", g_t))
    sketch.addConstraint(Constraint("Vertical", g_r))
    sketch.addConstraint(Constraint("Vertical", g_l))
    # Sketcher GeoId -1 / pos 1 = sketch origin point.
    sketch.addConstraint(Constraint("Symmetric", g_b, 1, g_t, 1, -1, 1))
    cw = sketch.addConstraint(Constraint("DistanceX", g_b, 1, g_b, 2, width))
    sketch.renameConstraint(cw, "Width")
    ch = sketch.addConstraint(Constraint("DistanceY", g_r, 1, g_r, 2, height))
    sketch.renameConstraint(ch, "Height")
    _bind_expression(sketch, "Constraints.Width", varset, width_var)
    _bind_expression(sketch, "Constraints.Height", varset, height_var)
    return {"bottom": g_b, "right": g_r, "top": g_t, "left": g_l}


def add_circle(sketch, cx=0.0, cy=0.0, radius=None,
               varset=None, radius_var=None, diameter_var=None,
               radius_expr=None, name="Radius"):
    """Add a circle. Radius binding (precedence: radius_expr > diameter_var > radius_var):

    - `radius_expr="HeadDiameter / 2"` → bind constraint to that expression
    - `diameter_var="HeadDiameter"`    → bind to `HeadDiameter / 2` (helper adds `/ 2`)
    - `radius_var="OuterRadius"`       → bind to `OuterRadius`
    - `radius=2.5`                     → no binding, literal only
    """
    if radius_expr is None and diameter_var is not None:
        radius_expr = f"{diameter_var} / 2"
    if radius_expr is None and radius_var is not None:
        radius_expr = radius_var
    if radius is None:
        if varset is not None and diameter_var is not None:
            radius = float(getattr(varset, diameter_var)) / 2.0
        elif varset is not None and radius_var is not None:
            radius = float(getattr(varset, radius_var))
        else:
            radius = 1.0
    g = sketch.addGeometry(
        Part.Circle(App.Vector(cx, cy, 0), App.Vector(0, 0, 1), radius), False
    )
    if abs(cx) < 1e-9 and abs(cy) < 1e-9:
        sketch.addConstraint(Constraint("Coincident", g, 3, -1, 1))
    cr = sketch.addConstraint(Constraint("Radius", g, radius))
    sketch.renameConstraint(cr, name)
    _bind_expression(sketch, f"Constraints.{name}", varset, radius_expr)
    return g


def add_regular_polygon(sketch, n, circumradius, name_prefix="Hex"):
    """Add a regular n-gon centered at origin. Returns list of line edge ids.

    A guide construction circle holds the named radius constraint
    (`<name_prefix>Radius`); n line segments whose endpoints sit on the
    circle, with equal-length sides + a rotation lock, complete the
    polygon. Editing the named radius reshapes the whole polygon.
    """
    if n < 3:
        raise ValueError(f"polygon needs >= 3 sides, got {n}")
    cc = sketch.addGeometry(
        Part.Circle(App.Vector(0, 0, 0), App.Vector(0, 0, 1), circumradius), True
    )
    sketch.addConstraint(Constraint("Coincident", cc, 3, -1, 1))
    cr = sketch.addConstraint(Constraint("Radius", cc, circumradius))
    sketch.renameConstraint(cr, f"{name_prefix}Radius")
    # First vertex on +Y for visual sanity (and for the rotation lock below).
    angles = [2 * math.pi * i / n + math.pi / 2 for i in range(n)]
    pts = [App.Vector(circumradius * math.cos(a), circumradius * math.sin(a), 0)
           for a in angles]
    edges = []
    for i in range(n):
        g = sketch.addGeometry(Part.LineSegment(pts[i], pts[(i + 1) % n]), False)
        edges.append(g)
    for i in range(n):
        sketch.addConstraint(Constraint("Coincident", edges[i], 2, edges[(i + 1) % n], 1))
    for eid in edges:
        sketch.addConstraint(Constraint("PointOnObject", eid, 1, cc))
    for i in range(1, n):
        sketch.addConstraint(Constraint("Equal", edges[0], edges[i]))
    # Rotation lock: vertex 0 on the Y axis. Using DistanceX=0 instead of
    # PointOnObject(...,V_axis) because the V-axis GeoId disagrees across
    # FreeCAD versions.
    sketch.addConstraint(Constraint("DistanceX", -1, 1, edges[0], 1, 0.0))
    return edges


def add_4_hole_pattern(sketch, varset, spacing_var, diameter_var, name_prefix="Hole"):
    """Four circles at corners ±spacing/2 with diameter bound to a VarSet
    property. Used for mounting-hole patterns (NEMA-style flanges, etc.).
    Returns list of geometry handles.
    """
    spacing = float(getattr(varset, spacing_var))
    radius = float(getattr(varset, diameter_var)) / 2.0
    half = f"<<{varset.Label}>>.{spacing_var} / 2"
    handles = []
    for i, (sx, sy) in enumerate([(+1, +1), (+1, -1), (-1, +1), (-1, -1)]):
        cx, cy = sx * spacing / 2.0, sy * spacing / 2.0
        g = sketch.addGeometry(
            Part.Circle(App.Vector(cx, cy, 0), App.Vector(0, 0, 1), radius), False
        )
        cdx = sketch.addConstraint(Constraint("DistanceX", -1, 1, g, 3, cx))
        sketch.renameConstraint(cdx, f"{name_prefix}X{i}")
        cdy = sketch.addConstraint(Constraint("DistanceY", -1, 1, g, 3, cy))
        sketch.renameConstraint(cdy, f"{name_prefix}Y{i}")
        cr = sketch.addConstraint(Constraint("Radius", g, radius))
        sketch.renameConstraint(cr, f"{name_prefix}R{i}")
        # Negation must wrap the whole `<<vs>>.<prop>` ref — `<<vs>>.-prop`
        # is not a valid expression. So compose these directly instead of
        # going through _bind_expression.
        sketch.setExpression(
            f"Constraints.{name_prefix}X{i}", half if sx > 0 else f"-({half})"
        )
        sketch.setExpression(
            f"Constraints.{name_prefix}Y{i}", half if sy > 0 else f"-({half})"
        )
        _bind_expression(
            sketch, f"Constraints.{name_prefix}R{i}", varset,
            f"{diameter_var} / 2",
        )
        handles.append(g)
    return handles


# ─── Pad / Pocket / length binding ────────────────────────────────────


def bind_length(feature, varset, var_name):
    """Bind a feature's `Length` property to a VarSet expression."""
    feature.setExpression("Length", f"<<{varset.Label}>>.{var_name}")


def make_pad(body, sketch, name, varset=None, length_var=None, length=None):
    """Create a PartDesign::Pad and (optionally) bind its Length to a VarSet."""
    pad = body.newObject("PartDesign::Pad", name)
    pad.Profile = sketch
    if length is None and varset is not None and length_var is not None:
        length = float(getattr(varset, length_var))
    pad.Length = length
    if varset is not None and length_var is not None:
        bind_length(pad, varset, length_var)
    return pad


def make_pocket(body, sketch, name, *, through_all=False, reversed_=False,
                varset=None, length_var=None, length=None):
    """Create a PartDesign::Pocket. Pass `through_all=True` for a through cut;
    otherwise a finite Length is bound (literal or via VarSet). `reversed_`
    flips the cut direction — needed when the sketch sits on the XY base
    plane and the pad extends +Z.
    """
    pocket = body.newObject("PartDesign::Pocket", name)
    pocket.Profile = sketch
    if through_all:
        pocket.Type = "ThroughAll"
    else:
        if length is None and varset is not None and length_var is not None:
            length = float(getattr(varset, length_var))
        pocket.Length = length
        if varset is not None and length_var is not None:
            bind_length(pocket, varset, length_var)
    if reversed_:
        pocket.Reversed = True
    return pocket


# ─── Headless edge / face selection ──────────────────────────────────


def find_vertical_edges(shape, tol=1e-6):
    """Return Edge<i> refs for edges parallel to Z (both endpoints share x,y)."""
    refs = []
    for i, edge in enumerate(shape.Edges, start=1):
        verts = edge.Vertexes
        if len(verts) != 2:
            continue
        p1, p2 = verts[0].Point, verts[1].Point
        if (abs(p1.x - p2.x) < tol
                and abs(p1.y - p2.y) < tol
                and abs(p1.z - p2.z) > tol):
            refs.append(f"Edge{i}")
    return refs


def find_face_by_position(shape, target, tol=0.5):
    """Return the Face<i> ref whose centre-of-mass is closest to `target`.

    `target` is an (x, y, z) tuple. Used in lieu of GUI face-clicking.
    """
    target_v = App.Vector(*target)
    best_i, best_d = None, float("inf")
    for i, face in enumerate(shape.Faces, start=1):
        d = (face.CenterOfMass - target_v).Length
        if d < best_d:
            best_d, best_i = d, i
    if best_i is None:
        raise RuntimeError("no faces in shape")
    if best_d > tol:
        App.Console.PrintWarning(
            f"find_face_by_position: closest face dist={best_d:.3f} > tol={tol}\n"
        )
    return f"Face{best_i}"
