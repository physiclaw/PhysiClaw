"""Tests for hardware/manual/build_extrusion_drawing.py — the cut list's
join to the BOM, the section outlines traced from the CAD constants, and
the rendered sheet."""

from __future__ import annotations

import re

import pytest

from hardware.manual import BuildError
from hardware.manual import build_extrusion_drawing as bed
from hardware.parts import _fits
from hardware.parts.standard import extrusion_spec as spec


def bom_rows(overrides: dict[str, dict] | None = None) -> list[dict]:
    """One BOM row per spec, stating the model's length in both languages;
    ``overrides`` replaces a row's fields, keyed by part_id."""
    rows = []
    for cut in bed.SPECS:
        row = {
            "part_id": cut.part_id,
            "spec": {
                "en": f"Length {cut.length:g} mm",
                "zh": f"长度 {cut.length:g} mm",
            },
            "qty": "2",
            "desc": {"en": f"use of {cut.part_id}", "zh": "用途"},
        }
        row.update((overrides or {}).get(cut.part_id, {}))
        rows.append(row)
    return rows


# ── cut list ──────────────────────────────────────────────────────────────────


def test_cut_list_joins_each_spec_to_its_bom_row_in_spec_order():
    items = bed.cut_list(bom_rows({"ext-2040-x": {"qty": "5"}}))

    assert [(i.spec.part_id, i.qty) for i in items] == [
        ("ext-2040-y", 2),
        ("ext-2040-x", 5),
        ("ext-1020-x-beam", 2),
        ("ext-1020-phone-support", 2),
    ]
    assert items[0].application == {"en": "use of ext-2040-y", "zh": "用途"}


def test_cut_list_reads_the_real_bom():
    items = bed.cut_list()

    assert sum(i.qty for i in items) == 7


def test_cut_list_refuses_a_bom_row_that_states_another_length():
    rows = bom_rows(
        {"ext-2040-y": {"spec": {"en": "Length 999 mm", "zh": "长度 999 mm"}}}
    )

    with pytest.raises(
        BuildError, match=r"'ext-2040-y' spec \[en\] does not state '345 mm'"
    ):
        bed.cut_list(rows)


def test_cut_list_checks_the_chinese_spec_too():
    rows = bom_rows(
        {"ext-2040-x": {"spec": {"en": "Length 190 mm", "zh": "长度 191 mm"}}}
    )

    with pytest.raises(BuildError, match=r"spec \[zh\] does not state '190 mm'"):
        bed.cut_list(rows)


def test_cut_list_refuses_a_missing_bom_row():
    rows = [r for r in bom_rows() if r["part_id"] != "ext-1020-x-beam"]

    with pytest.raises(BuildError, match="no BOM row with part_id 'ext-1020-x-beam'"):
        bed.cut_list(rows)


def test_cut_list_refuses_a_non_numeric_qty():
    with pytest.raises(BuildError, match="qty 'two' is not a number"):
        bed.cut_list(bom_rows({"ext-2040-y": {"qty": "two"}}))


# ── the counterbore and tap are the standard ones for the frame screw ─────────


def test_counterbore_is_the_standard_clearance_for_the_frame_screw():
    assert spec.frame_screw == "M6" and spec.cb_shaft_d == _fits.M6_NORMAL
    assert (spec.cb_head_d, spec.cb_head_depth) == (11.0, 6.8)  # DIN 974-1


def test_counterbore_and_tap_match_the_screw_tables():
    pytest.importorskip("build123d")  # the screw tables load the CAD kernel
    from hardware.parts.standard.screw import COMMON, SHCS_DIMS

    head, thread = SHCS_DIMS[spec.frame_screw], COMMON[spec.frame_screw]

    assert spec.cb_head_d > head["dk"] and spec.cb_head_depth > head["k"]
    # The tapped end: the profile's bore is the tap drill for the coarse
    # thread, and the thread runs at least 1.5 d into the aluminum.
    assert spec.end_tap == spec.frame_screw
    assert spec.end_tap_pitch == thread["P"]
    assert spec.bore_diameter == thread["d"] - thread["P"]
    assert spec.end_tap_depth >= 1.5 * thread["d"]


# ── section outlines ──────────────────────────────────────────────────────────


def bbox(pts):
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (min(xs), max(xs), min(ys), max(ys))


def mirrored(pts, fn):
    return {(round(a, 6), round(b, 6)) for a, b in (fn(x, y) for x, y in pts)}


def test_2040_outline_spans_two_cells_and_is_mirror_symmetric():
    pts = bed.outline_2040()

    assert bbox(pts) == (-2 * spec.leg, 2 * spec.leg, -spec.leg, spec.leg)
    assert all(not bed._same(a, b) for a, b in zip(pts, pts[1:] + pts[:1]))
    for fn in (lambda x, y: (-x, y), lambda x, y: (x, -y)):
        assert mirrored(pts, fn) == mirrored(pts, lambda x, y: (x, y))


def test_2040_channel_sits_inside_the_profile():
    floor = spec.cell_offset - spec.wedge_vertices[1][0]

    assert bbox(bed.channel_2040()) == (
        -floor,
        floor,
        -spec.slot_h / 2,
        spec.slot_h / 2,
    )
    assert spec.slot_h / 2 < spec.leg


def test_2040_channel_refuses_a_spec_it_cannot_trace(monkeypatch):
    monkeypatch.setattr(spec, "slot_w", 2 * spec.leg)

    with pytest.raises(BuildError, match="channel rectangle no longer meets"):
        bed.channel_2040()


def test_1020_outline_is_the_half_profile_mirrored_and_centred():
    pts = bed.outline_1020()
    hx, hy = spec.half_x_1020, spec.half_x_1020 / 2

    assert bbox(pts) == (-hx, hx, -hy, hy)
    assert len(pts) == 2 * len(spec.half_vertices_1020) - 2


# ── words ─────────────────────────────────────────────────────────────────────


def test_machining_sentences_carry_the_spec_constants():
    assert bed.machining_text(bed.CB, "en") == (
        "4×M6 counterbore (two per end): Ø6.6 through, Ø11 × 6.8 deep, "
        "on the 40 mm face, 10 mm from the end · for M6 socket head cap screws"
    )
    assert bed.machining_text(bed.CB, "zh").endswith("配合 M6 圆柱头螺栓使用")
    assert (
        bed.machining_text(bed.TAP, "zh")
        == "两端各 2 个中心孔（Ø5 底孔）攻丝 M6×1-6H，深 12 mm"
    )
    assert bed.callout_lines(bed.TAP, "en") == (
        "2×M6×1-6H, 12 deep",
        "both ends",
        "Ø5 bore = tap drill",
    )
    assert bed.callout_lines(bed.HOLE, "en") == (
        "2×Ø5.5 through · square to the slot face",
    )


# ── table wrapping ────────────────────────────────────────────────────────────


def test_wrap_breaks_at_sentence_joints_never_inside_a_dimension():
    en = "4× M6 counterbore: Ø6.6 through, Ø11 × 6.8 deep · 40 mm face · for M6 screws"
    lines = bed.wrap(
        en,
        bed.text_width(
            "4× M6 counterbore: Ø6.6 through, Ø11 × 6.8 deep · 40 mm", bed.TD_FS
        ),
        bed.TD_FS,
    )

    assert lines == [
        "4× M6 counterbore: Ø6.6 through, Ø11 × 6.8 deep",
        "· 40 mm face · for M6 screws",
    ]
    zh = "沉孔 Ø11 深 6.8，开在 40 mm 宽面，配合 M6 圆柱头螺栓使用"
    assert bed.wrap(
        zh, bed.text_width("沉孔 Ø11 深 6.8，开在 40 mm 宽面，", bed.TD_FS), bed.TD_FS
    ) == [
        "沉孔 Ø11 深 6.8，开在 40 mm 宽面，",
        "配合 M6 圆柱头螺栓使用",
    ]


def test_wrap_leaves_a_fitting_or_unbreakable_string_alone():
    assert bed.wrap("short", 100, bed.TD_FS) == ["short"]
    assert bed.wrap("unbreakable-run-of-text", 5, bed.TD_FS) == [
        "unbreakable-run-of-text"
    ]


# ── the sheet ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("lang", "pages"), [("en", 2), ("zh", 1)])
def test_document_has_a4_landscape_sheets_with_every_length(lang, pages):
    doc = bed.render_document(bed.cut_list(bom_rows()), lang)

    sheets = re.findall(r"<svg\b.*?</svg>", doc, re.DOTALL)
    assert len(sheets) == pages
    assert "@page { size: A4 landscape; margin: 0; }" in doc
    assert f'<html lang="{ {"en": "en", "zh": "zh-Hans"}[lang] }">' in doc
    for sheet in sheets:
        assert 'width="297mm" height="210mm"' in sheet
        for cut in bed.SPECS:
            assert f">{cut.length:g} mm<" in sheet  # the cut-list cell
            assert f">{cut.length:g}<" in sheet  # the length dimension
    assert "Ø11" in doc and "M6" in doc
    # Standard sections are dimensioned by their nominal size, never the
    # model's measured 19.8 × 9.9.
    assert ">19.8<" not in doc and ">9.9<" not in doc
    assert ">20<" in doc and ">10<" in doc and ">40<" in doc


def test_english_document_appends_the_chinese_sheet_with_original_footers():
    items = bed.cut_list(bom_rows())
    en = bed.render_document(items, "en")
    zh = bed.render_document(items, "zh")
    english, chinese = re.findall(r"<svg\b.*?</svg>", en, re.DOTALL)
    standalone = re.search(r"<svg\b.*?</svg>", zh, re.DOTALL).group()

    assert "Aluminum extrusion tech drawing" in english
    assert "铝型材加工图" not in english
    assert "Sheet 1 of 1" in english
    assert "第 1 页，共 1 页" in chinese
    assert chinese == standalone.replace(bed.DEFS, "")
    assert ".sheet + .sheet { break-before: page; }" in en
    assert "break-before" not in zh
    # IDs are document-wide, including across separate inline SVG sheets.
    ids = re.findall(r'\bid="([^"]+)"', en)
    assert len(ids) == len(set(ids))
    assert set(re.findall(r"url\(#([^)]+)\)", en)) <= set(ids)


def test_chinese_sheet_is_localized():
    doc = bed.render_document(bed.cut_list(bom_rows()), "zh")

    assert (
        "铝型材加工图" in doc
        and "攻丝 M6×1-6H" in doc
        and "Aluminum extrusion" not in doc
    )


def test_document_is_byte_stable():
    items = bed.cut_list(bom_rows())

    assert bed.render_document(items, "en") == bed.render_document(items, "en")


def test_build_writes_the_html_per_language_and_skips_pdf_without_chrome(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(bed, "find_chrome", lambda: None)

    written = bed.build(["en", "zh"], tmp_path, pdf=True)

    assert [p.name for p in written] == [
        bed.LANG_FILENAME["en"],
        bed.LANG_FILENAME["zh"],
    ]
    assert all(
        p.read_text(encoding="utf-8").startswith("<!DOCTYPE html>") for p in written
    )
    assert "no Chrome/Chromium found" in capsys.readouterr().out


@pytest.mark.parametrize("langs", [["en"], ["zh"], ["en", "zh"]])
def test_build_prints_the_same_sheets_as_the_selected_html(
    tmp_path, monkeypatch, langs
):
    printed = {}

    def fake_render(html: str, pdf_path, chrome: str) -> bool:
        printed[pdf_path.name] = html
        pdf_path.write_bytes(b"%PDF")
        return True

    monkeypatch.setattr(bed, "find_chrome", lambda: "chrome")
    monkeypatch.setattr(bed, "render_pdf", fake_render)

    written = bed.build(langs, tmp_path, pdf=True)

    assert [p.name for p in written] == [
        name
        for lang in langs
        for name in (bed.LANG_FILENAME[lang], bed.PDF_FILENAME[lang])
    ]
    for lang in langs:
        document = (tmp_path / bed.LANG_FILENAME[lang]).read_text(encoding="utf-8")
        assert printed[bed.PDF_FILENAME[lang]] == document
        assert document.count("<svg") == (2 if lang == "en" else 1)


def test_output_names_are_ascii_release_asset_names():
    assert bed.PDF_FILENAME["zh"] == "physiclaw_extrusion_drawing_zh.pdf"
    assert all(name.isascii() for name in bed.LANG_FILENAME.values())
