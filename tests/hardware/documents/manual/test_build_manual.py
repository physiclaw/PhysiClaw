"""Tests for hardware/documents/manual/build.py — the pure document helpers."""

from __future__ import annotations

import json
import sys
from contextlib import nullcontext
from unittest.mock import Mock

import pytest

from hardware import buildlog
from hardware.documents import BuildError
from hardware.documents.manual import assets as manual_assets
from hardware.documents.manual import build as build_manual
from hardware.documents.manual.assets import InlineAssets


@pytest.mark.parametrize("browser", ["chrome", "edge"])
def test_pdf_request_requires_browser_before_removing_existing_manual(
    tmp_path, monkeypatch, browser
):
    path = tmp_path / "previous.pdf"
    path.write_bytes(b"previous")
    monkeypatch.setattr(build_manual, "find_browser", lambda selected: None)
    with pytest.raises(BuildError, match=f"browser '{browser}' was not found"):
        build_manual.build(["en"], tmp_path, inline=False, pdf=True, browser=browser)
    assert path.read_bytes() == b"previous"


@pytest.mark.parametrize("browser", ["chrome", "edge"])
def test_pdf_uses_selected_browser(tmp_path, monkeypatch, browser):
    executable = str(tmp_path / f"{browser}.exe")
    finder = Mock(return_value=executable)
    renderer = Mock(return_value=True)
    monkeypatch.setattr(build_manual, "find_browser", finder)
    monkeypatch.setattr(build_manual, "render_pdf", renderer)
    monkeypatch.setattr(build_manual, "prepare_figures", lambda: None)
    monkeypatch.setattr(build_manual, "load_pages", lambda: [])

    build_manual.build(["en", "zh"], tmp_path, inline=False, pdf=True, browser=browser)

    finder.assert_called_once_with(browser)
    assert renderer.call_count == 2
    assert all(call.args[2] == executable for call in renderer.call_args_list)


def test_html_only_does_not_require_selected_browser(tmp_path, monkeypatch):
    finder = Mock(side_effect=AssertionError("HTML builds must not look for browsers"))
    monkeypatch.setattr(build_manual, "find_browser", finder)
    monkeypatch.setattr(build_manual, "prepare_figures", lambda: None)
    monkeypatch.setattr(build_manual, "load_pages", lambda: [])

    paths = build_manual.build(["en"], tmp_path, inline=False, browser="edge")

    assert paths == [tmp_path / "physiclaw_manual.html"]
    assert paths[0].exists()
    finder.assert_not_called()


@pytest.mark.parametrize(
    "arguments,browser", [([], "chrome"), (["--browser", "edge"], "edge")]
)
def test_cli_browser_default_and_override(tmp_path, monkeypatch, arguments, browser):
    builder = Mock(return_value=[])
    monkeypatch.setattr(build_manual, "build", builder)
    monkeypatch.setattr(buildlog, "command", lambda *a, **kw: nullcontext(Mock()))
    monkeypatch.setattr(
        sys, "argv", ["manual", "--pdf", "--out", str(tmp_path), *arguments]
    )

    build_manual.main.__wrapped__()

    builder.assert_called_once_with(
        ["en", "zh"], tmp_path.resolve(), inline=False, pdf=True, browser=browser
    )


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
    monkeypatch.setattr(manual_assets, "SVG_DIR", tmp_path / "svg")
    monkeypatch.setattr(manual_assets, "HAND_FIGURES", {})

    with buildlog.session(tmp_path) as log:
        paths = build_manual.build(["en", "zh"], tmp_path / "out", inline=False)

    output = capsys.readouterr()
    assert "warning:" not in output.out + output.err
    saved = [json.loads(line) for line in log.path.read_text().splitlines()]
    warnings = [record for record in saved if record["event"] == "style_warning"]
    assert len(warnings) == 1
    warning = warnings[0]["message"]
    assert "page 'wire_splice'.notes[0].style" in warning
    assert "'top:140'" in warning
    assert "browsers ignore this declaration" in warning
    assert len(paths) == 2
    assert all(path.exists() for path in paths)
