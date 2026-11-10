"""Errors for missing length units in authored manual layout styles."""

from __future__ import annotations

import pytest

from hardware.documents.manual.pages import load_pages
from hardware.documents.manual.style_checks import inline_style_unit_errors


def errors_for(style: str) -> list[str]:
    return list(
        inline_style_unit_errors([{"page": "wire_splice", "notes": [{"style": style}]}])
    )


@pytest.mark.parametrize(
    "style",
    [
        "top:140",
        "left:-10;",
        "width:.5",
        "height:+2.5",
        "max-width:1e2",
        "padding:0 4",
        "margin:1 2 auto",
        "gap:4!important",
        "TOP: 140 !IMPORTANT",
        "top:140 /* missing mm */",
        "margin:calc(100% - 10mm) 8",
        "padding:var(--gap, calc(2 * 1mm)) 8",
        "border-radius:10px/4",
        "border-radius:4/10px",
        "width:1e999999999999999999999999999999",
        "width:1e-999999999999999999999999999999",
        "margin:0/**/8",
        'width:var(--width, "text; top:140;"); left:8',
        "--example:{width:80; nested:[(top:140;)]}; left:8",
    ],
)
def test_missing_units_report_page_field_and_declaration(style):
    errors = errors_for(style)
    assert len(errors) == 1
    assert "page 'wire_splice'.notes[0].style" in errors[0]
    assert "non-zero length without a CSS unit" in errors[0]
    assert "mm or px" in errors[0]


@pytest.mark.parametrize(
    "style",
    [
        "top:140mm; left:10px; width:50%; height:2rem",
        "top:0; left:-0; margin:0.0 +0 .0 0e2",
        "z-index:2; opacity:0.9; line-height:1.5; font-weight:400; flex:1",
        "top:auto; width:inherit; max-width:none; margin:0 auto",
        "top:calc(100% - 10mm); width:var(--width, 100px)",
        "margin:calc(var(--gap) * 2) 0",
        "transform:translate(10mm, 20mm) scale(0.85)",
        "--offset:140; /* top:140; */ left:10mm",
        '--example: "text; width:80;"; left:10mm',
        "--example: 'text; width:80;'; left:10mm",
        r'--example: "escaped \"; width:80;"; left:10mm',
        'background:url("data:image/svg+xml;text; width:80;"); left:10mm',
        "--example:{width:80; nested:[(top:140;)]}; left:10mm",
        "border-radius:10px/4px",
        "border-radius:10px/0",
        "width:0e999999999999999999999999999999",
        "width:-0.0e-999999999999999999999999999999",
        "left:10mm; /* width:80;",
        r"width:1\70 x",
    ],
)
def test_valid_lengths_and_unitless_numbers_are_accepted(style):
    assert errors_for(style) == []


def test_diagnostics_preserve_original_declarations():
    errors = errors_for('--example:"text; width:80;"; margin:calc(2 * 1mm) 8 /* mm */')
    assert len(errors) == 1
    assert repr("margin:calc(2 * 1mm) 8 /* mm */") in errors[0]


def test_all_rendered_style_fields_are_checked_recursively():
    pages = [
        {
            "page": "example",
            "gridStyle": "gap:4",
            "figures": [{"style": "width:100"}],
            "notes": [{"h3Style": "font-size:12", "body": {"en": "top:140"}}],
            "bom": {"style": "right:16"},
        }
    ]
    errors = list(inline_style_unit_errors(pages))
    assert len(errors) == 4
    assert all(
        any(field in error for error in errors)
        for field in (
            ".gridStyle",
            ".figures[0].style",
            ".notes[0].h3Style",
            ".bom.style",
        )
    )


def test_current_manual_content_has_no_missing_units():
    assert load_pages()
