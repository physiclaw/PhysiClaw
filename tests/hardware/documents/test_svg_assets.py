"""Document assets reflect edits without restarting the Python process."""

from __future__ import annotations

import base64
import os
from xml.etree import ElementTree

import pytest

from hardware.documents import assets
from hardware.documents.manual import assets as manual_assets
from hardware.documents.manual import build as manual


@pytest.fixture
def asset_root(tmp_path, monkeypatch):
    for folder in ("icon", "figure"):
        (tmp_path / folder).mkdir()
    monkeypatch.setattr(assets, "__file__", str(tmp_path / "__init__.py"))
    yield tmp_path
    manual_assets._read_svg.cache_clear()


@pytest.mark.parametrize(
    ("read_svg", "folder", "filename"),
    [
        (lambda: assets.icon.crab_svg, "icon", "crab.svg"),
        (lambda: assets.figure.wire_splice_svg, "figure", "wire_splice.svg"),
    ],
)
def test_svg_edits_replacements_and_removal_are_not_cached(
    asset_root, read_svg, folder, filename
):
    directory = asset_root / folder
    path = directory / filename
    path.write_text("<svg>first</svg>", encoding="utf-8")
    assert read_svg() == "<svg>first</svg>"

    previous = path.stat()
    path.write_text("<svg>other</svg>", encoding="utf-8")
    os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    assert read_svg() == "<svg>other</svg>"

    replacement = directory / "replacement.svg"
    replacement.write_text("<svg>third</svg>", encoding="utf-8")
    replacement.replace(path)
    assert read_svg() == "<svg>third</svg>"

    path.unlink()
    with pytest.raises(FileNotFoundError):
        read_svg()


def test_placeholder_treats_label_as_text():
    label = '<bolt & "nut">'
    root = ElementTree.fromstring(assets.icon.hardware_placeholder_svg(label))
    text = root.find("{http://www.w3.org/2000/svg}text")
    assert text is not None
    assert text.text == label
    assert len(root) == 2  # The rectangle and text; labels cannot add SVG nodes.


@pytest.mark.parametrize("inline", [False, True], ids=["external", "inline"])
def test_rebuilding_manual_refreshes_icons_figures_and_render_cache(
    asset_root, monkeypatch, inline
):
    renders = asset_root / "renders"
    renders.mkdir()
    icons = asset_root / "icon"
    figures = asset_root / "figure"
    (icons / "crab.svg").write_text("<svg>logo</svg>", encoding="utf-8")
    monkeypatch.setattr(manual_assets, "SVG_DIR", renders)
    monkeypatch.setattr(
        manual,
        "load_pages",
        lambda: [
            {
                "type": "solo",
                "page": "figures",
                "figures": [{"src": "wire_splice.svg"}, {"src": "frame.svg"}],
                "notes": [
                    {"icon": "info", "classes": "note", "h3": "Info", "body": "Note"}
                ],
            }
        ],
    )

    wire_path = figures / "wire_splice.svg"
    for version in ("first", "other"):
        info = f"<svg>{version}-info</svg>"
        wire = f"<svg>{version}-wire</svg>"
        frame = f"<svg>{version}-frame</svg>"
        (icons / "info.svg").write_text(info, encoding="utf-8")
        previous = wire_path.stat() if wire_path.exists() else None
        wire_path.write_text(wire, encoding="utf-8")
        if previous is not None:
            os.utime(wire_path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        (renders / "frame.svg").write_text(frame, encoding="utf-8")

        paths = manual.build(["en", "zh"], asset_root / "out", inline=inline)
        for path in paths:
            document = path.read_text(encoding="utf-8")
            assert info in document
            for name, content in (("wire_splice.svg", wire), ("frame.svg", frame)):
                if inline:
                    encoded = base64.b64encode(content.encode()).decode("ascii")
                    assert f"data:image/svg+xml;base64,{encoded}" in document
                else:
                    assert f'src="assets/{name}"' in document
                    assert (path.parent / "assets" / name).read_text(
                        encoding="utf-8"
                    ) == content
        assert (renders / "wire_splice.svg").read_text(encoding="utf-8") == wire
