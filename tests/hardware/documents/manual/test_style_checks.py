"""Warnings for missing length units in authored manual layout styles."""

from __future__ import annotations

import pytest

from hardware.documents.manual.pages import load_pages
from hardware.documents.manual.style_checks import inline_style_unit_warnings


def warnings_for(style: str) -> list[str]:
    return list(
        inline_style_unit_warnings(
            [{"page": "wire_splice", "notes": [{"style": style}]}]
        )
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
    ],
)
def test_missing_units_warn_with_page_field_and_declaration(style):
    warnings = warnings_for(style)
    assert len(warnings) == 1
    assert "page 'wire_splice'.notes[0].style" in warnings[0]
    assert "non-zero length without a CSS unit" in warnings[0]
    assert "mm or px" in warnings[0]


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
    ],
)
def test_valid_lengths_and_unitless_numbers_do_not_warn(style):
    assert warnings_for(style) == []


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
    warnings = list(inline_style_unit_warnings(pages))
    assert len(warnings) == 4
    assert all(
        any(field in warning for warning in warnings)
        for field in (
            ".gridStyle",
            ".figures[0].style",
            ".notes[0].h3Style",
            ".bom.style",
        )
    )


def test_current_manual_content_has_no_missing_units():
    assert list(inline_style_unit_warnings(load_pages())) == []
