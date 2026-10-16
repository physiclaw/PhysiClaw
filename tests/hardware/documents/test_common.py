"""Tests for hardware/documents/common.py — shared builder helpers."""

from __future__ import annotations

import pytest

from hardware.documents import common

# ── loc ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "lang", "expected"),
    [
        ({"en": "Frame", "zh": "框架"}, "zh", "框架"),
        ({"en": "Frame", "zh": "框架"}, "en", "Frame"),
        ({"en": "Frame", "zh": ""}, "zh", "Frame"),
        ({"en": "Frame"}, "zh", "Frame"),
        ("M6×16", "zh", "M6×16"),
    ],
    ids=["zh", "en", "empty-zh-falls-back", "missing-zh-falls-back", "plain-string"],
)
def test_loc_resolves_language_with_english_fallback(value, lang, expected):
    assert common.loc(value, lang) == expected


# ── _rowspans ─────────────────────────────────────────────────────────────────


def test_rowspans_marks_group_starts_with_size_and_rest_with_zero():
    rows = [{"k": "a"}, {"k": "a"}, {"k": "b"}, {"k": "a"}]

    assert common._rowspans(rows, lambda r: r["k"]) == [2, 0, 1, 1]


def test_rowspans_of_empty_rows_is_empty():
    assert common._rowspans([], lambda r: r) == []


NBSP = "\u00a0"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("sits 2 mm clear", f'sits <span class="unit">2{NBSP}mm</span> clear'),
        ("4 screws", f"4{NBSP}screws"),
        ("12 V 2 A", f"12{NBSP}V 2{NBSP}A"),
        ("100 µm", f'<span class="unit">100{NBSP}µm</span>'),
        ("90 °C", f"90{NBSP}°C"),
        ("留 2 mm。", f'留 <span class="unit">2{NBSP}mm</span>。'),
        ("2 到 3 小时", f"2{NBSP}到 3{NBSP}小时"),
        ("M3×8", "M3×8"),
        ("0 0 1.5mm", "0 0 1.5mm"),
    ],
)
def test_typeset_quantities_joins_a_number_to_the_word_after_it(text, expected):
    assert common.typeset_quantities(f"<p>{text}</p>") == f"<p>{expected}</p>"


def test_typeset_quantities_leaves_attributes_and_inline_css_alone():
    markup = '<div style="margin: 0 auto; left:110mm"><img alt="4 parts">2 mm</div>'
    assert common.typeset_quantities(markup) == (
        f'<div style="margin: 0 auto; left:110mm"><img alt="4 parts"><span class="unit">2{NBSP}mm</span></div>'
    )


def test_typeset_quantities_marks_a_unit_symbol_to_keep_its_case_under_capitals():
    markup = "<h3>75 Hz at 220 mm</h3><p>0.5 mm, 4 screws</p><h3>3 wires</h3>"
    assert common.typeset_quantities(markup) == (
        f'<h3><span class="unit">75{NBSP}Hz</span> at <span class="unit">220{NBSP}mm</span></h3>'
        f'<p><span class="unit">0.5{NBSP}mm</span>, 4{NBSP}screws</p><h3>3{NBSP}wires</h3>'
    )
