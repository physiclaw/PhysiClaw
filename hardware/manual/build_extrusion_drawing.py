"""Render A4 landscape cut-and-drill drawings for the frame's seven extrusions.

Dimensions and profiles use the CAD constants in ``travel_ranges`` and
``extrusion_spec``; quantities and applications come from ``11_bom.json``.
The build rejects BOM lengths that disagree with the CAD.

English output: EN then ZH (2 pages). Chinese output: ZH (1 page).
Each sheet keeps its "1 of 1" footer. Sections and details are 1:1;
machined faces are 1:2.

Run ``python -m hardware drawing`` for HTML; add ``--pdf`` for headless
Chrome PDFs. HTML is deterministic and stamped with ``MANUAL_VERSION``.
"""

from __future__ import annotations

import argparse
import html
import sys
from dataclasses import dataclass
from pathlib import Path

from hardware.assembly.travel_ranges import (
    PHONE_BED_BEAM_LENGTH,
    X_BEAM_LENGTH,
    X_EXTRUSION_LENGTH,
    Y_EXTRUSION_LENGTH,
)
from hardware.manual import BuildError
from hardware.manual.build_sourcing_guide import load_bom_rows
from hardware.manual.common import HTML_LANG, URL_MARK, _step, loc, manual_version
from hardware.manual.pdf import find_chrome, render_pdf
from hardware.parts.standard import extrusion_spec as spec
from hardware.scheme import OUTPUT_DIR as _OUTPUT_ROOT

# --------------------------------------------------------------------------- #
# Paths and names
# --------------------------------------------------------------------------- #
# Own output dir, beside the sourcing guide's — the release packages both.
OUTPUT_DIR = _OUTPUT_ROOT / "drawing"

# ASCII names in both languages, unlike the manual and the sourcing guide:
# the PDFs are published as direct release assets (the sourcing guide's
# note links them), and a release asset's name must survive GitHub's
# asset-name rules, which the 中文 manual filename would not.
LANG_FILENAME = {
    "en": "physiclaw_extrusion_drawing_en.html",
    "zh": "physiclaw_extrusion_drawing_zh.html",
}
PDF_FILENAME = {
    lang: str(Path(name).with_suffix(".pdf")) for lang, name in LANG_FILENAME.items()
}


# --------------------------------------------------------------------------- #
# The cut list — what the supplier machines, joined to the BOM rows
# --------------------------------------------------------------------------- #
# Machining kinds; each has one table sentence and one drawing row.
CB, TAP, PLAIN, HOLE = "cb", "tap", "plain", "hole"


@dataclass(frozen=True)
class Profile:
    """A standard profile as the sheet presents it: dimensioned by the
    nominal size its designation means (the 1020 is modelled at its
    measured 19.8 × 9.9, but sold as 20 × 10) and ordered with a finish."""

    nominal: tuple[float, float]  # width × height
    size: tuple[float, float]  # the model's section, width × height
    finish: dict | str  # as the BOM orders it; "—" when unspecified

    @property
    def heading(self) -> str:
        return f"{fmt(self.nominal[0])} × {fmt(self.nominal[1])}"


PROFILES = {
    "2040": Profile(
        spec.nominal_2040,
        (2 * (spec.cell_offset + spec.leg), 2 * spec.leg),
        {"en": "Black anodized", "zh": "黑色阳极氧化"},
    ),
    "1020": Profile(spec.nominal_1020, (2 * spec.half_x_1020, spec.half_x_1020), "—"),
}


@dataclass(frozen=True)
class CutSpec:
    """One extrusion specification as the model cuts it. ``part_id`` names
    the BOM row that carries the quantity and the application."""

    part_id: str
    profile: str  # a PROFILES key
    length: float
    machining: str


SPECS: tuple[CutSpec, ...] = (
    CutSpec("ext-2040-y", "2040", Y_EXTRUSION_LENGTH, CB),
    CutSpec("ext-2040-x", "2040", X_EXTRUSION_LENGTH, TAP),
    CutSpec("ext-1020-x-beam", "1020", X_BEAM_LENGTH, PLAIN),
    CutSpec("ext-1020-phone-support", "1020", PHONE_BED_BEAM_LENGTH, HOLE),
)


@dataclass(frozen=True)
class CutItem:
    """A cut-list line: the spec plus what its BOM row says about it."""

    spec: CutSpec
    qty: int
    application: dict | str


def fmt(value: float) -> str:
    """A millimetre value as drawn: no trailing zeros (345, 5.5, 19.8)."""
    return f"{value:g}"


def cut_list(rows: list[dict] | None = None) -> list[CutItem]:
    """The four specifications joined to their BOM rows, in SPECS order.

    Raises ``BuildError`` when a spec's BOM row is missing, or when the
    row's spec text (either language) no longer states the length the model
    cuts — the BOM is hand-written, so this is where a travel-range edit
    that was not carried into the manual surfaces."""
    rows = load_bom_rows() if rows is None else rows
    by_id = {r.get("part_id"): r for r in rows}
    items: list[CutItem] = []
    for cut in SPECS:
        row = by_id.get(cut.part_id)
        if row is None:
            raise BuildError(f"no BOM row with part_id {cut.part_id!r}")
        stated = f"{fmt(cut.length)} mm"
        for lang in ("en", "zh"):
            if stated not in loc(row.get("spec", ""), lang):
                raise BuildError(
                    f"BOM row {cut.part_id!r} spec [{lang}] does not state "
                    f"{stated!r} — the model cuts {stated} "
                    f"(hardware/assembly/travel_ranges.py); update 11_bom.json"
                )
        try:
            qty = int(row.get("qty", ""))
        except ValueError:
            raise BuildError(
                f"BOM row {cut.part_id!r} qty {row.get('qty')!r} is not a number"
            ) from None
        items.append(CutItem(cut, qty, row.get("desc", "")))
    return items


# --------------------------------------------------------------------------- #
# Section outlines — traced from the CAD's profile vertices
# --------------------------------------------------------------------------- #
Point = tuple[float, float]

# The square's eight symmetries in order round the profile, each with
# whether it reverses the traversal direction (the reflections do). The
# 2020 cell's eighth-wedge chain runs from the +X slot floor to the
# top-right corner; its images under these, joined in this order, are the
# cell's boundary.
SYMMETRIES = (
    (lambda x, y: (x, y), False),
    (lambda x, y: (y, x), True),
    (lambda x, y: (-y, x), False),
    (lambda x, y: (-x, y), True),
    (lambda x, y: (-x, -y), False),
    (lambda x, y: (-y, -x), True),
    (lambda x, y: (y, -x), False),
    (lambda x, y: (x, -y), True),
)


def _trace(chain, groups) -> list[Point]:
    """Join the images of ``chain`` under ``groups`` — (symmetry, offset)
    pairs, in order — into one closed polygon. Each image continues from
    the last (a shared seam vertex is dropped), as is the closing vertex."""
    pts: list[Point] = []
    for (fn, reverse), (dx, dy) in groups:
        image = [fn(x, y) for x, y in chain]
        if reverse:
            image.reverse()
        image = [(x + dx, y + dy) for x, y in image]
        if pts and _same(image[0], pts[-1]):
            image = image[1:]
        pts.extend(image)
    if _same(pts[0], pts[-1]):
        pts.pop()
    return pts


def _same(a: Point, b: Point) -> bool:
    return abs(a[0] - b[0]) < 1e-9 and abs(a[1] - b[1]) < 1e-9


def outline_2040() -> list[Point]:
    """The 2040's outer boundary: two 2020 cells at ±cell_offset, joined
    where their facing slots meet. The right cell contributes its six
    outward chains (from the bottom seam round to the top seam), the left
    cell its six; the facing-slot chains become the channel."""
    chain = spec.wedge_vertices[1:]  # the centre point is interior
    right = [(SYMMETRIES[k], (spec.cell_offset, 0.0)) for k in (5, 6, 7, 0, 1, 2)]
    left = [(SYMMETRIES[k], (-spec.cell_offset, 0.0)) for k in (1, 2, 3, 4, 5, 6)]
    return _trace(chain, right + left)


def channel_2040() -> list[Point]:
    """The 2040's central channel: the two facing T-slot voids merged with
    the rectangular through-channel the model subtracts. The rectangle's
    sides must fall on the voids' flat cavity tops and its height must stay
    inside the cell, which is what makes this a sixteen-vertex polygon; the
    spec is checked for that rather than trusted."""
    w1, w2, w3, w4 = spec.wedge_vertices[1:5]
    floor = spec.cell_offset - w1[0]  # slot floor, from the profile centre
    belly = spec.cell_offset - w3[0]  # where the rib meets the cavity top
    inner = spec.cell_offset - w4[0]  # where the cavity top ends
    hx, hy = spec.slot_w / 2, spec.slot_h / 2
    if not (inner < hx < belly and w3[1] < hy < spec.leg):
        raise BuildError(
            "the 2040 channel rectangle no longer meets the T-slot voids where "
            "this drawing traces it — extrusion_spec changed; revisit channel_2040()"
        )
    top = w3[1]
    return [
        (-floor, -w2[1]),
        (-floor, w2[1]),
        (-belly, top),
        (-hx, top),
        (-hx, hy),
        (hx, hy),
        (hx, top),
        (belly, top),
        (floor, w2[1]),
        (floor, -w2[1]),
        (belly, -top),
        (hx, -top),
        (hx, -hy),
        (-hx, -hy),
        (-hx, -top),
        (-belly, -top),
    ]


def outline_1020() -> list[Point]:
    """The 1020's boundary, centred like the 2040's: the right half-profile
    and its mirror, shifted down by half the height (the CAD's native Y=0
    is the bottom face)."""
    half = spec.half_vertices_1020
    dy = spec.half_x_1020 / 2
    right = [(x, y - dy) for x, y in half]
    return right + [(-x, y) for x, y in reversed(right[1:-1])]


# --------------------------------------------------------------------------- #
# Localized strings
# --------------------------------------------------------------------------- #
UI: dict[str, dict[str, str]] = {
    "doc_title": {
        "en": "PhysiClaw.ai Aluminum Extrusion Tech Drawing",
        "zh": "PhysiClaw.ai 铝型材加工图",
    },
    "h1": {"en": "Aluminum extrusion tech drawing", "zh": "铝型材加工图"},
    "subtitle": {
        "en": "Cut-to-length and drilling for the PhysiClaw frame · "
        "{pieces} pieces, {specs} specifications · EU-standard T-slot profiles",
        "zh": "PhysiClaw 框架铝型材下料与钻孔 · 共 {pieces} 根，{specs} 种规格 · 欧标 T 型槽铝型材",
    },
    "unit": {"en": "Dimensions in mm", "zh": "单位：mm"},
    "no_scale": {"en": "Do not scale the drawing", "zh": "以标注尺寸为准，请勿量图"},
    "th_no": {"en": "No.", "zh": "序号"},
    "th_profile": {"en": "Profile", "zh": "型材"},
    "th_finish": {"en": "Finish", "zh": "表面处理"},
    "th_length": {"en": "Length", "zh": "长度"},
    "th_qty": {"en": "Qty", "zh": "数量"},
    "th_machining": {"en": "Machining", "zh": "加工要求"},
    "th_application": {"en": "Application", "zh": "用途"},
    "total_label": {"en": "Total", "zh": "合计"},
    "total": {"en": "{pieces} pieces", "zh": "{pieces} 根"},
    "section": {"en": "Section 1:1", "zh": "截面 1:1"},
    "face_40": {"en": "40 mm face · 1:2", "zh": "40 mm 宽面 · 1:2"},
    "face_slot": {"en": "Slot face · 1:2", "zh": "槽面 · 1:2"},
    "detail_a": {"en": "A · counterbore section 1:1", "zh": "A · 沉孔剖面 1:1"},
    "end_view": {"en": "End view · 1:1", "zh": "端面 · 1:1"},
    "notes": {"en": "Notes", "zh": "技术要求"},
    "note_tol": {
        "en": "1. General tolerances ISO 2768-m.",
        "zh": "1. 未注公差按 GB/T 1804-m。",
    },
    "note_deburr": {
        "en": "2. Cut square; deburr all edges.",
        "zh": "2. 切口平整垂直，全部去毛刺。",
    },
    "footer": {
        "en": "Rev. {version} · Sheet 1 of 1",
        "zh": "版本 {version} · 第 1 页，共 1 页",
    },
}

# Table sentences and drawing callouts per machining kind, filled from the
# spec constants so a diameter edit reaches both the words and the picture.
MACHINING = {
    CB: {
        "en": "4×{screw} counterbore (two per end): Ø{shaft} through, Ø{head} × {depth} deep, "
        "on the 40 mm face, {off} mm from the end · for {screw} socket head cap screws",
        "zh": "4×{screw} 沉孔（每端 2 个）：Ø{shaft} 通孔，沉孔 Ø{head} 深 {depth}，"
        "开在 40 mm 宽面，距端面 {off} mm，配合 {screw} 圆柱头螺栓使用",
    },
    TAP: {
        "en": "Both ends: tap the two center bores (Ø{bore} tap drill) {thread}, {tap_depth} mm deep",
        "zh": "两端各 2 个中心孔（Ø{bore} 底孔）攻丝 {thread}，深 {tap_depth} mm",
    },
    PLAIN: {"en": "Cut to length only", "zh": "仅下料"},
    HOLE: {
        "en": "2×Ø{d} through, square to the slot face, centered, {off} mm from each end",
        "zh": "2×Ø{d} 通孔，垂直槽面钻穿，位于截面中心，距两端 {off} mm",
    },
}
CALLOUT = {
    CB: {
        "en": (
            "4×{screw} counterbore",
            "Ø{shaft} through · Ø{head} × {depth} deep",
            "see section A",
        ),
        "zh": ("4×{screw} 沉孔", "Ø{shaft} 通孔 · 沉孔 Ø{head} 深 {depth}", "见剖面 A"),
    },
    TAP: {
        "en": ("2×{thread}, {tap_depth} deep", "both ends", "Ø{bore} bore = tap drill"),
        "zh": (
            "2×{thread} 攻丝，深 {tap_depth}",
            "两端相同",
            "底孔为型材 Ø{bore} 中心孔",
        ),
    },
    HOLE: {
        "en": ("2×Ø{d} through · square to the slot face",),
        "zh": ("2×Ø{d} 通孔 · 垂直槽面钻穿",),
    },
}
FILL = {
    "screw": spec.frame_screw,
    "shaft": fmt(spec.cb_shaft_d),
    "head": fmt(spec.cb_head_d),
    "depth": fmt(spec.cb_head_depth),
    "off": fmt(spec.cb_end_offset),
    "thread": f"{spec.end_tap}×{fmt(spec.end_tap_pitch)}-{spec.end_tap_class}",
    "tap_depth": fmt(spec.end_tap_depth),
    "bore": fmt(spec.bore_diameter),
    "d": fmt(spec.end_hole_d),
}


def ui(key: str, lang: str, **kw: object) -> str:
    return loc(UI[key], lang).format(**kw)


def machining_text(kind: str, lang: str) -> str:
    return loc(MACHINING[kind], lang).format(**FILL)


def callout_lines(kind: str, lang: str) -> tuple[str, ...]:
    return tuple(line.format(**FILL) for line in CALLOUT[kind][lang])


# --------------------------------------------------------------------------- #
# SVG primitives — the page is drawn in millimetres (1 user unit = 1 mm)
# --------------------------------------------------------------------------- #
PAGE_W, PAGE_H = 297.0, 210.0  # A4 landscape
MARGIN = 8.0
ELEV = 0.5  # the machined-face views are drawn at 1:2

ARROW = 2.4  # dimension arrowhead length, mm
DIM_GAP = 0.8  # extension line stands off the feature by this
DIM_OVER = 1.2  # and overshoots the dimension line by this
NARROW = 9.0  # spans under this get their arrows outside
DIM_STANDOFF = 5.0  # a face view's dimension lines stand off its plate by this
VIEW_LABEL_DROP = 6.5  # a view's label baseline below its lowest edge

# Arrowhead markers a dimension line carries.
ARROW_END = 'marker-end="url(#arrow)"'
ARROW_BOTH = 'marker-start="url(#arrow)" marker-end="url(#arrow)"'


def esc(text: str) -> str:
    return html.escape(text, quote=True)


def n(value: float) -> str:
    """A coordinate: three decimals, trailing zeros stripped, no '-0'."""
    out = f"{value:.3f}".rstrip("0").rstrip(".")
    return "0" if out in ("-0", "") else out


def text(
    x: float,
    y: float,
    s: str,
    cls: str,
    anchor: str = "middle",
    rotate: float = 0,
) -> str:
    tr = f' transform="rotate({n(rotate)} {n(x)} {n(y)})"' if rotate else ""
    return f'<text class="{cls}" x="{n(x)}" y="{n(y)}" text-anchor="{anchor}"{tr}>{esc(s)}</text>'


def multiline(
    x: float, y: float, lines: tuple[str, ...], cls: str, anchor: str, lead: float
) -> str:
    return "".join(text(x, y + i * lead, s, cls, anchor) for i, s in enumerate(lines))


def line(
    x1: float, y1: float, x2: float, y2: float, cls: str = "thin", markers: str = ""
) -> str:
    attrs = f" {markers}" if markers else ""
    return f'<line class="{cls}"{attrs} x1="{n(x1)}" y1="{n(y1)}" x2="{n(x2)}" y2="{n(y2)}"/>'


def rect(x: float, y: float, w: float, h: float, cls: str = "outline") -> str:
    return f'<rect class="{cls}" x="{n(x)}" y="{n(y)}" width="{n(w)}" height="{n(h)}"/>'


def circle(cx: float, cy: float, r: float, cls: str = "outline") -> str:
    return f'<circle class="{cls}" cx="{n(cx)}" cy="{n(cy)}" r="{n(r)}"/>'


def polygon(pts: list[Point], cls: str = "outline") -> str:
    return f'<polygon class="{cls}" points="{" ".join(f"{n(x)},{n(y)}" for x, y in pts)}"/>'


def placed(pts: list[Point], ox: float, oy: float, scale: float) -> list[Point]:
    """Model points (Y up, origin at the profile centre) onto the page
    (Y down) at ``scale``, centred on (ox, oy)."""
    return [(ox + x * scale, oy - y * scale) for x, y in pts]


def _dim(
    a1: float, a2: float, at: float, feat: float, label: str, vertical: bool
) -> str:
    """A dimension between a1 and a2 along one axis, its line at ``at`` on
    the other, extension lines from the feature edge at ``feat``. Narrow
    spans get their arrows outside. The value sits above a horizontal line
    and reads bottom-to-top left of a vertical one."""

    def pt(along: float, across: float) -> tuple[float, float]:
        return (across, along) if vertical else (along, across)

    side = -1 if at < feat else 1  # direction from the feature to the line
    out = [
        line(*pt(a1, feat + side * DIM_GAP), *pt(a1, at + side * DIM_OVER)),
        line(*pt(a2, feat + side * DIM_GAP), *pt(a2, at + side * DIM_OVER)),
    ]
    # The value sits centred on the span when it clears the extension lines
    # by the usual gap; otherwise it goes outside, beyond the end arrow (right
    # of a horizontal line, above a vertical one), and the arrows go outside
    # with it so it has a line to sit beside.
    fits = text_width(label, DIMTEXT_FS) + 2 * DIM_GAP <= a2 - a1
    if a2 - a1 < NARROW or not fits:
        out.append(line(*pt(a1 - 2 * ARROW, at), *pt(a1, at), "dim", ARROW_END))
        out.append(line(*pt(a2 + 2 * ARROW, at), *pt(a2, at), "dim", ARROW_END))
    else:
        out.append(line(*pt(a1, at), *pt(a2, at), "dim", ARROW_BOTH))
    if fits:
        along, anchor = (a1 + a2) / 2, "middle"
    else:
        outside = a1 - 2 * ARROW - DIM_GAP if vertical else a2 + 2 * ARROW + DIM_GAP
        along, anchor = outside, "start"
    rotate = -90 if vertical else 0
    out.append(text(*pt(along, at - 0.9), label, "dimtext mono", anchor, rotate))
    return "".join(out)


def dim_h(x1: float, x2: float, y: float, label: str, feat_y: float) -> str:
    return _dim(x1, x2, y, feat_y, label, vertical=False)


def dim_v(x: float, y1: float, y2: float, label: str, feat_x: float) -> str:
    return _dim(y1, y2, x, feat_x, label, vertical=True)


def leader(fx: float, fy: float, tx: float, ty: float, lines: tuple[str, ...]) -> str:
    """A leader from the feature (fx, fy) to a text block starting at (tx, ty)."""
    anchor = "start" if tx >= fx else "end"
    shelf = tx + (3 if anchor == "start" else -3)
    return (
        f'<circle class="dot" cx="{n(fx)}" cy="{n(fy)}" r="0.5"/>'
        + line(fx, fy, tx, ty)
        + line(tx, ty, shelf, ty)
        + multiline(
            shelf + (0.6 if anchor == "start" else -0.6),
            ty - 0.6,
            lines,
            "note",
            anchor,
            3.0,
        )
    )


# --------------------------------------------------------------------------- #
# The views
# --------------------------------------------------------------------------- #
def profile_2040(ox: float, oy: float) -> str:
    """The 2040 section at 1:1 centred on (ox, oy): outline, channel, bores."""
    return (
        polygon(placed(outline_2040(), ox, oy, 1), "solid")
        + polygon(placed(channel_2040(), ox, oy, 1), "void")
        + circle(ox - spec.cell_offset, oy, spec.bore_diameter / 2, "void")
        + circle(ox + spec.cell_offset, oy, spec.bore_diameter / 2, "void")
    )


def profile_1020(ox: float, oy: float) -> str:
    """The 1020 section at 1:1 centred on (ox, oy): outline and rim holes."""
    rx = spec.half_x_1020 - spec.hole_1020_x_inset
    ry = spec.hole_1020_y_inset - spec.half_x_1020 / 2
    return (
        polygon(placed(outline_1020(), ox, oy, 1), "solid")
        + circle(ox + rx, oy - ry, spec.hole_1020_d / 2, "void")
        + circle(ox - rx, oy - ry, spec.hole_1020_d / 2, "void")
    )


def overall_dims(ox: float, oy: float, profile: Profile) -> str:
    """Width above and height beside a section at 1:1, labelled with the
    profile's nominal size."""
    (w, h), (nw, nh) = profile.size, profile.nominal
    return dim_h(ox - w / 2, ox + w / 2, oy - h / 2 - 4, fmt(nw), oy - h / 2) + dim_v(
        ox + w / 2 + 4, oy - h / 2, oy + h / 2, fmt(nh), ox + w / 2
    )


def section_2040(ox: float, oy: float) -> str:
    return profile_2040(ox, oy) + overall_dims(ox, oy, PROFILES["2040"])


def section_1020(ox: float, oy: float) -> str:
    return profile_1020(ox, oy) + overall_dims(ox, oy, PROFILES["1020"])


def face_plate(x0: float, y0: float, length: float, W: float, profile: Profile) -> str:
    """A machined-face view's plate at 1:2 — the rectangle, the length
    dimension above and the nominal width beside."""
    L = length * ELEV
    return (
        rect(x0, y0, L, W)
        + dim_h(x0, x0 + L, y0 - DIM_STANDOFF, fmt(length), y0)
        + dim_v(x0 + L + DIM_STANDOFF, y0, y0 + W, fmt(profile.nominal[0]), x0 + L)
    )


def end_offset_dims(x0: float, L: float, y: float, offset: float) -> str:
    """The two 'offset from the end' dimensions under a face view."""
    off = offset * ELEV
    at = y + DIM_STANDOFF
    return dim_h(x0, x0 + off, at, fmt(offset), y) + dim_h(
        x0 + L - off, x0 + L, at, fmt(offset), y
    )


# A view in a drawing row: its SVG and the y where its ink ends, which the
# sheet lays the rows out from.
Ink = tuple[str, float]


def face_2040_cb(x0: float, y0: float, length: float, lang: str) -> Ink:
    """The long 2040's 40 mm face at 1:2: four counterbores, dimensioned
    from the ends and across the width, with the callout."""
    L, W = length * ELEV, PROFILES["2040"].size[0] * ELEV
    off, pitch = spec.cb_end_offset * ELEV, 2 * spec.cell_offset * ELEV
    rows = (y0 + W / 2 - pitch / 2, y0 + W / 2 + pitch / 2)
    cols = (x0 + off, x0 + L - off)
    r = spec.cb_head_d / 2 * ELEV
    parts = [face_plate(x0, y0, length, W, PROFILES["2040"])]
    for cx in cols:
        for cy in rows:
            parts.append(circle(cx, cy, r))
            parts.append(circle(cx, cy, spec.cb_shaft_d / 2 * ELEV))
    parts.append(end_offset_dims(x0, L, y0 + W, spec.cb_end_offset))
    parts.append(dim_v(x0 - 5, rows[0], rows[1], fmt(2 * spec.cell_offset), x0))
    parts.append(
        leader(
            cols[0] + r * 0.7,
            rows[1] - r * 0.7,
            cols[0] + 14,
            y0 + W / 2 + 0.6,
            callout_lines(CB, lang),
        )
    )
    return "".join(parts), y0 + W + DIM_STANDOFF + DIM_OVER


def detail_counterbore(ox: float, oy: float, lang: str) -> Ink:
    """Section A at 1:1: a cut through one counterbore, looking along the
    extrusion — the 20 mm wall with the head pocket and the through
    shaft."""
    wall_w, wall_h = 2 * spec.cb_head_d, 2 * spec.leg  # a slice of the wall
    x0, y0 = ox - wall_w / 2, oy - wall_h / 2
    pocket_w, pocket_d, shaft_w = spec.cb_head_d, spec.cb_head_depth, spec.cb_shaft_d
    # The cut outline: wall top, down the pocket, the shaft, out the bottom.
    left = [
        (x0, y0),
        (ox - pocket_w / 2, y0),
        (ox - pocket_w / 2, y0 + pocket_d),
        (ox - shaft_w / 2, y0 + pocket_d),
        (ox - shaft_w / 2, y0 + wall_h),
        (x0, y0 + wall_h),
    ]
    # Two hatched halves, mirrored about the hole axis, so the pocket and
    # the shaft read as the cut between them.
    right = [(2 * ox - x, y) for x, y in reversed(left)]
    label_y = y0 + wall_h + 9.5
    svg = (
        polygon(left, "cut")
        + polygon(right, "cut")
        + dim_h(ox - pocket_w / 2, ox + pocket_w / 2, y0 - 4, f"Ø{fmt(pocket_w)}", y0)
        + dim_h(
            ox - shaft_w / 2,
            ox + shaft_w / 2,
            y0 + wall_h + 4,
            f"Ø{fmt(shaft_w)}",
            y0 + wall_h,
        )
        + dim_v(x0 + wall_w + 4, y0, y0 + pocket_d, fmt(pocket_d), x0 + wall_w)
        + text(ox, label_y, ui("detail_a", lang), "label")
    )
    return svg, label_y


def face_2040_tap(x0: float, y0: float, length: float) -> Ink:
    """The short 2040's face at 1:2 — cut only, so just the plate."""
    W = PROFILES["2040"].size[0] * ELEV
    return face_plate(x0, y0, length, W, PROFILES["2040"]), y0 + W


def end_2040_tap(ox: float, oy: float, lang: str) -> Ink:
    """The short 2040's end at 1:1 with the two tapped bores called out."""
    parts = [profile_2040(ox, oy)]
    for sx in (-1, 1):
        parts.append(
            circle(ox + sx * spec.cell_offset, oy, spec.bore_diameter / 2 + 0.9, "thin")
        )
    parts.append(
        leader(
            ox + spec.cell_offset + 2.2,
            oy - 2.2,
            ox + spec.cell_offset + 12,
            oy - 5,
            callout_lines(TAP, lang),
        )
    )
    label_y = oy + spec.leg + VIEW_LABEL_DROP
    parts.append(text(ox, label_y, ui("end_view", lang), "label"))
    return "".join(parts), label_y


def face_1020(x0: float, y0: float, length: float, lang: str, holes: bool) -> Ink:
    """A 1020's slot face at 1:2: the slot mouth as two lines, and for the
    phone-bed beam the two vertical end holes with their callout."""
    L, W = length * ELEV, PROFILES["1020"].size[0] * ELEV
    mouth = 2 * spec.half_vertices_1020[3][0] * ELEV  # the top-face opening
    parts = [
        face_plate(x0, y0, length, W, PROFILES["1020"]),
        line(x0, y0 + W / 2 - mouth / 2, x0 + L, y0 + W / 2 - mouth / 2, "outline"),
        line(x0, y0 + W / 2 + mouth / 2, x0 + L, y0 + W / 2 + mouth / 2, "outline"),
    ]
    if holes:
        off, r = spec.end_hole_offset * ELEV, spec.end_hole_d / 2 * ELEV
        for cx in (x0 + off, x0 + L - off):
            parts.append(circle(cx, y0 + W / 2, r))
        parts.append(end_offset_dims(x0, L, y0 + W, spec.end_hole_offset))
        # Below the plate: above it, the band between the top edge and the
        # length dimension is too narrow for a line of text.
        parts.append(
            leader(
                x0 + off + r * 0.7,
                y0 + W / 2 + r * 0.7,
                x0 + off + 14,
                y0 + W + 4.5,
                callout_lines(HOLE, lang),
            )
        )
        return "".join(parts), y0 + W + DIM_STANDOFF + DIM_OVER
    return "".join(parts), y0 + W


def end_1020_hole(ox: float, oy: float, lang: str) -> Ink:
    """The phone-bed beam's end at 1:1, the vertical hole shown hidden
    through the full height so 'through' is unambiguous."""
    h, r = spec.half_x_1020, spec.end_hole_d / 2
    label_y = oy + h / 2 + VIEW_LABEL_DROP
    svg = (
        profile_1020(ox, oy)
        + line(ox - r, oy - h / 2, ox - r, oy + h / 2, "hidden")
        + line(ox + r, oy - h / 2, ox + r, oy + h / 2, "hidden")
        + dim_h(ox - r, ox + r, oy - h / 2 - 4, f"Ø{fmt(spec.end_hole_d)}", oy - h / 2)
        + text(ox, label_y, ui("end_view", lang), "label")
    )
    return svg, label_y


# --------------------------------------------------------------------------- #
# The sheet
# --------------------------------------------------------------------------- #
CSS = """\
@page { size: A4 landscape; margin: 0; }
html, body { margin: 0; padding: 0; background: #eaeaea; }
.sheet { width: 297mm; height: 210mm; margin: 12mm auto; background: #fff;
         box-shadow: 0 2px 12px rgba(0,0,0,.18); display: block; }
@media print { html, body { background: #fff; } .sheet { margin: 0; box-shadow: none; } }
svg { font-family: "Inter", "Helvetica Neue", "Segoe UI", "PingFang SC", "Hiragino Sans GB",
      "Noto Sans SC", "Noto Sans CJK SC", "Microsoft YaHei", Arial, sans-serif; fill: #1f1f1f; }
.mono { font-family: "JetBrains Mono", "SF Mono", Menlo, Consolas, monospace; }
/* Line weights: outlines and section fills thick, dimensions and leaders thin. */
.solid, .void, .cut, .outline { stroke: #1f1f1f; stroke-width: 0.35; stroke-linejoin: round; }
.solid { fill: #f3f3f3; }
.void { fill: #fff; }
.cut { fill: url(#hatch); }
.outline { fill: none; }
.thin, .dim, .hidden { fill: none; stroke: #1f1f1f; stroke-width: 0.18; }
.hidden { stroke-dasharray: 1.2 0.6; }
.dot { fill: #1f1f1f; }
.rule { fill: none; stroke: #b8b8b8; stroke-width: 0.25; }
.frame { fill: none; stroke: #1f1f1f; stroke-width: 0.5; }
/* Type sizes, mm. */
.h1 { font-size: 6px; fill: #d4511a; font-weight: 500; letter-spacing: 0.02em; }
.sub { font-size: 2.9px; fill: #6a6a6a; }
.brand { font-size: 3px; fill: #b8b8b8; letter-spacing: 0.12em; }
.meta, .foot { font-size: 2.6px; fill: #6a6a6a; }
.foot { font-size: 2.4px; }
.th { font-size: 2.4px; fill: #6a6a6a; text-transform: uppercase; letter-spacing: 0.08em; }
.td { font-size: 2.9px; }
.group { font-size: 3.6px; fill: #d4511a; font-weight: 500; }
.no { font-size: 3.2px; font-weight: 600; }
.label { font-size: 2.5px; fill: #6a6a6a; }
.dimtext { font-size: 2.5px; }
.note { font-size: 2.6px; }
.notes { font-size: 2.5px; }
.notes-title { font-size: 2.6px; font-weight: 600; }
"""

# Type sizes the layout measures with, mm — mirrored from the CSS above.
TD_FS = 2.9  # .td
LABEL_FS = 2.5  # .label
DIMTEXT_FS = 2.5  # .dimtext
NOTES_FS = 2.5  # .notes
FOOT_FS = 2.4  # .foot

DEFS = (
    '<defs><marker id="arrow" viewBox="0 0 10 10" refX="10" refY="5" '
    f'markerWidth="{n(ARROW)}" markerHeight="{n(ARROW)}" markerUnits="userSpaceOnUse" '
    'orient="auto-start-reverse"><path d="M0 1.2 L10 5 L0 8.8 z" fill="#1f1f1f"/></marker>'
    # Section hatching for the cut faces of the counterbore detail.
    '<pattern id="hatch" patternUnits="userSpaceOnUse" width="1.4" height="1.4" '
    'patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="1.4" stroke="#1f1f1f" '
    'stroke-width="0.14"/></pattern></defs>'
)

# Cut-list column x positions (left edges) across the 281 mm content width.
COLS = (0.0, 10.0, 34.0, 62.0, 84.0, 98.0, 236.0)
TD_LEAD = 3.3  # line pitch inside a wrapped cell


def text_width(s: str, size: float) -> float:
    """A rough advance width: a CJK glyph is an em wide, a Latin glyph
    about 0.6 em — enough to decide where a table cell must wrap."""
    return sum(size if ord(c) > 0x2E7F else 0.6 * size for c in s)


def wrap(s: str, width: float, size: float) -> list[str]:
    """Break ``s`` into lines no wider than ``width``. Each break goes at
    the last sentence joint that fits — before a ' · ' separator, after a
    comma or a Chinese comma / semicolon — and only when no joint fits, at
    the last space; so a value never parts from its unit ('10 mm') while a
    joint is available."""
    lines: list[str] = []
    rest = s
    while text_width(rest, size) > width:
        fits = [i for i in range(1, len(rest)) if text_width(rest[:i], size) <= width]
        cut = max((i for i in fits if _joint(rest, i)), default=None)
        if cut is None:
            cut = max((i for i in fits if rest[i] == " "), default=None)
        if cut is None:
            break
        lines.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    lines.append(rest)
    return lines


def _joint(s: str, i: int) -> bool:
    return s.startswith(" · ", i) or s[i - 1] in "，；" or s.startswith(", ", i - 1)


def render_table(
    items: list[CutItem], x: float, y: float, w: float, lang: str
) -> tuple[str, float]:
    """The cut list; returns the SVG and the y just below it. The machining
    sentence wraps within its column and the row grows to hold it."""
    row_h, head_h = 5.8, 5.2
    machining_w = COLS[6] - COLS[5] - 1.6
    parts = [line(x, y, x + w, y, "rule")]
    heads = (
        "th_no",
        "th_profile",
        "th_finish",
        "th_length",
        "th_qty",
        "th_machining",
        "th_application",
    )
    for cx, key in zip(COLS, heads):
        parts.append(text(x + cx + 0.6, y + 3.9, ui(key, lang), "th", "start"))
    yy = y + head_h
    parts.append(line(x, yy, x + w, yy, "rule"))
    for no, it in enumerate(items, start=1):
        base = yy + row_h - 2.0
        machining = wrap(machining_text(it.spec.machining, lang), machining_w, TD_FS)
        cells = (
            (0, str(no), "no"),
            (1, it.spec.profile, "td mono"),
            (2, loc(PROFILES[it.spec.profile].finish, lang), "td"),
            (3, f"{fmt(it.spec.length)} mm", "td mono"),
            (4, f"{it.qty}", "td mono"),
            (6, loc(it.application, lang), "td"),
        )
        for col, s, cls in cells:
            parts.append(text(x + COLS[col] + 0.6, base, s, cls, "start"))
        for i, s in enumerate(machining):
            parts.append(text(x + COLS[5] + 0.6, base + i * TD_LEAD, s, "td", "start"))
        yy += row_h + (len(machining) - 1) * TD_LEAD
        parts.append(line(x, yy, x + w, yy, "rule"))
    # The count starts flush with the Qty column, its label a space before.
    pieces = sum(it.qty for it in items)
    count_x, base = x + COLS[4] + 0.6, yy + 3.9
    label_x = count_x - text_width(" ", TD_FS)
    parts.append(text(label_x, base, ui("total_label", lang), "td", "end"))
    parts.append(
        text(count_x, base, ui("total", lang, pieces=pieces), "td mono", "start")
    )
    return "".join(parts), yy + 5.0


def render_sheet(items: list[CutItem], lang: str, *, include_defs: bool = True) -> str:
    """The whole A4 page as one SVG; later sheets reuse the first sheet's defs."""
    x0, w = MARGIN, PAGE_W - 2 * MARGIN
    parts = [rect(x0, MARGIN, w, PAGE_H - 2 * MARGIN, "frame")]

    # Masthead.
    pieces, specs = sum(it.qty for it in items), len(items)
    y = MARGIN + 7
    parts.append(text(x0 + 4, y, ui("h1", lang), "h1", "start"))
    parts.append(text(x0 + w - 4, y, URL_MARK, "brand", "end"))
    parts.append(
        text(
            x0 + 4,
            y + 4.5,
            ui("subtitle", lang, pieces=pieces, specs=specs),
            "sub",
            "start",
        )
    )
    parts.append(
        text(
            x0 + w - 4,
            y + 4.5,
            f"{ui('unit', lang)} · {ui('no_scale', lang)}",
            "meta",
            "end",
        )
    )
    y += 8
    parts.append(line(x0, y, x0 + w, y, "frame"))

    # Cut list.
    table, y = render_table(items, x0 + 4, y + 2.5, w - 8, lang)
    parts.append(table)
    y += 1.5
    parts.append(line(x0, y, x0 + w, y, "frame"))

    # The drawing rows: a group per profile with its section at 1:1 on the
    # left, then one machined-face row per spec, numbered as in the table.
    number = {it.spec.machining: no for no, it in enumerate(items, start=1)}
    length = {it.spec.machining: it.spec.length for it in items}
    face_x = x0 + 66

    def group(gy: float, designation: str, section: str) -> None:
        heading = f"{designation} · {PROFILES[designation].heading}"
        parts.append(text(x0 + 4, gy, heading, "group", "start"))
        parts.append(text(x0 + 4, gy + 4.2, ui("section", lang), "label", "start"))
        parts.append(section)

    # A row: its number and label at ry, its views hung below. The views
    # report where their ink ends; a row is drawn once at ry = 0 to measure
    # it, then in place.
    PLATE = 8  # a face view's plate top below ry
    rise = 0.5 + LABEL_FS  # the row label's top above ry

    def row(ry: float, kind: str, label: str, views: list[Ink]) -> Ink:
        svg = (
            text(face_x - 2, ry + 3.2, str(number[kind]), "no", "end")
            + text(face_x, ry - 0.5, ui(label, lang), "label", "start")
            + "".join(svg for svg, _ in views)
        )
        return svg, max(end for _, end in views)

    rows = [
        (
            CB,
            "face_40",
            lambda ry: [
                face_2040_cb(face_x, ry + PLATE, length[CB], lang),
                detail_counterbore(x0 + w - 21, ry + 15, lang),
            ],
        ),
        (
            TAP,
            "face_40",
            lambda ry: [
                face_2040_tap(face_x, ry + PLATE, length[TAP]),
                end_2040_tap(x0 + w - 60, ry + 12, lang),
            ],
        ),
        (
            PLAIN,
            "face_slot",
            lambda ry: [
                face_1020(face_x, ry + PLATE, length[PLAIN], lang, holes=False)
            ],
        ),
        (
            HOLE,
            "face_slot",
            lambda ry: [
                face_1020(face_x, ry + PLATE, length[HOLE], lang, holes=True),
                end_1020_hole(x0 + w - 60, ry + 12, lang),
            ],
        ),
    ]
    heights = [rise + row(0, kind, label, make(0))[1] for kind, label, make in rows]

    # The footer is one line in the bottom-right corner.
    pad = 1.5
    fy = PAGE_H - MARGIN - pad
    parts.append(
        text(
            x0 + w - 4, fy, ui("footer", lang, version=manual_version()), "foot", "end"
        )
    )

    # The rows share the band between the cut list and the footer with equal
    # gaps. The rule between the two profiles splits the gap before row 3,
    # and each profile's heading sits level with its first row's label.
    gap = (fy - FOOT_FS - pad - y - sum(heights)) / 5
    if gap < 1:
        raise BuildError(f"drawing rows overflow the sheet (gap {gap:.1f} mm)")
    ry = y + gap + rise
    for i, ((kind, label, make), height) in enumerate(zip(rows, heights)):
        if i == 0:
            group(ry - 0.5, "2040", section_2040(x0 + 30, ry + 19.5))
        elif i == 2:
            rule = ry - rise - gap / 2
            parts.append(line(x0, rule, x0 + w, rule, "rule"))
            group(ry - 0.5, "1020", section_1020(x0 + 30, ry + 15.5))
            notes_y = ry + 34.5  # under the 1020 section, in the left column
        parts.append(row(ry, kind, label, make(ry))[0])
        ry += height + gap

    # Technical notes.
    parts.append(text(x0 + 4, notes_y, ui("notes", lang), "notes-title", "start"))
    for key in ("note_tol", "note_deburr"):
        for line_ in wrap(ui(key, lang), 52, NOTES_FS):
            notes_y += 3.4
            parts.append(text(x0 + 4, notes_y, line_, "notes", "start"))

    return (
        f'<svg class="sheet" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n(PAGE_W)} {n(PAGE_H)}" '
        f'width="{n(PAGE_W)}mm" height="{n(PAGE_H)}mm" role="img" aria-label="{esc(ui("doc_title", lang))}">'
        f"{DEFS if include_defs else ''}{''.join(parts)}</svg>"
    )


def render_document(items: list[CutItem], lang: str) -> str:
    sheets = render_sheet(items, lang)
    css = CSS
    if lang == "en":
        sheets += "\n" + render_sheet(items, "zh", include_defs=False)
        css += "@media print { .sheet + .sheet { break-before: page; } }\n"
    return (
        f'<!DOCTYPE html>\n<html lang="{HTML_LANG[lang]}">\n<head>\n'
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{esc(ui('doc_title', lang))}</title>\n"
        f"<style>\n{css}</style>\n</head>\n<body>\n{sheets}\n</body>\n</html>\n"
    )


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #
def build(langs: list[str], out_dir: Path, pdf: bool = False) -> list[Path]:
    """Write selected editions as HTML; optionally print PDFs if Chrome is available."""
    with _step("load cut list"):
        items = cut_list()
    chrome = find_chrome() if pdf else None
    if pdf and chrome is None:
        print("note: no Chrome/Chromium found — skipping PDF output")
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for lang in langs:
        path = out_dir / LANG_FILENAME[lang]
        with _step(f"render html [{lang}]"):
            doc = render_document(items, lang)
            path.write_text(doc, encoding="utf-8")
        written.append(path)
        if chrome:
            pdf_path = out_dir / PDF_FILENAME[lang]
            with _step(f"render pdf  [{lang}]"):
                ok = render_pdf(doc, pdf_path, chrome)
            if ok:
                written.append(pdf_path)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--lang",
        choices=("en", "zh", "all"),
        default="all",
        help="output edition: en includes English + Chinese; zh is Chinese only (default: all)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=OUTPUT_DIR,
        help="output directory (default: hardware/output/drawing)",
    )
    parser.add_argument(
        "--pdf",
        action="store_true",
        help="also render a PDF per language via headless Chrome (default: off — HTML only)",
    )
    args = parser.parse_args()

    langs = ["en", "zh"] if args.lang == "all" else [args.lang]
    out = args.out.resolve()
    shown = out.relative_to(Path.cwd()) if out.is_relative_to(Path.cwd()) else out
    print(f"building extrusion tech drawing [{', '.join(langs)}] -> {shown}")
    try:
        written = build(langs, out, pdf=args.pdf)
    except BuildError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    print(f"\ndone — wrote {len(written)} file(s):")
    for path in written:
        print(f"  {path.name}")


if __name__ == "__main__":
    main()
