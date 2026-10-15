"""Tests for hardware/manual/build_manual.py — the pure document helpers."""

from __future__ import annotations

import pytest

from hardware.manual import BuildError, build_manual
from hardware.manual.assets import InlineAssets


def test_pdf_request_requires_browser_before_removing_existing_manual(
    tmp_path, monkeypatch
):
    path = tmp_path / "previous.pdf"
    path.write_bytes(b"previous")
    monkeypatch.setattr(build_manual, "find_chrome", lambda: None)
    with pytest.raises(BuildError, match="no Chrome/Chromium"):
        build_manual.build(["en"], tmp_path, inline=False, pdf=True)
    assert path.read_bytes() == b"previous"


# ── figure_alt ────────────────────────────────────────────────────────────────


def test_figure_alt_prefers_a_pinned_alt():
    assert build_manual.figure_alt({"src": "x.svg", "alt": "the frame"}) == "the frame"


@pytest.mark.parametrize(
    ("src", "expected"),
    [
        (
            "frame_10_extrusion_tnut_exploded_cam0_rbun.svg",
            "frame_10_extrusion_tnut exploded",
        ),
        ("belt_20_clamp_assembled_cam1.svg", "belt_20_clamp assembled"),
    ],
    ids=["with-hash-token", "without-hash-token"],
)
def test_figure_alt_derives_readable_text_from_the_src(src, expected):
    assert build_manual.figure_alt({"src": src}) == expected


# ── cover helpers ─────────────────────────────────────────────────────────────


def test_cover_title_reads_the_cover_pages_localized_title():
    pages = [{"type": "cover", "title": {"en": "Assembly Manual", "zh": "装配手册"}}]

    title = build_manual.cover_title(pages, build_manual.Ctx("zh", InlineAssets()))

    assert title == "装配手册"


def test_cover_title_without_a_cover_falls_back_to_the_default():
    title = build_manual.cover_title([], build_manual.Ctx("en", InlineAssets()))

    assert title == "PhysiClaw Assembly Manual"


def test_build_warns_once_for_unitless_offset_and_still_writes_both_languages(
    tmp_path, monkeypatch, capsys
):
    pages = [
        {
            "type": "solo",
            "page": "wire_splice",
            "figures": [],
            "notes": [
                {
                    "h3": {"en": "Connect the wires", "zh": "连接"},
                    "body": {"en": "Connect each joint.", "zh": "连接各接头。"},
                    "classes": "note overlay sm",
                    "style": "left:16mm; top:140; width:70mm;",
                }
            ],
        }
    ]
    monkeypatch.setattr(build_manual, "load_pages", lambda: pages)
    monkeypatch.setattr(build_manual, "SVG_DIR", tmp_path / "svg")
    monkeypatch.setattr(build_manual, "HAND_FIGURES", {})

    paths = build_manual.build(["en", "zh"], tmp_path / "out", inline=False)

    warning = capsys.readouterr().err
    assert warning.count("warning:") == 1
    assert "page 'wire_splice'.notes[0].style" in warning
    assert "'top:140'" in warning
    assert "browsers ignore this declaration" in warning
    assert len(paths) == 2
    assert all(path.exists() for path in paths)
