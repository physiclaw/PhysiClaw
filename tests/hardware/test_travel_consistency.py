"""Change the real travel source in isolation and exercise downstream artifacts."""

import os
import py_compile
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from hardware.scheme import REPO_ROOT


def _reference_probe() -> dict:
    """Render both reference variants in a fresh, isolated CAD process."""
    import json

    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--illustrations"],
        env={
            **os.environ,
            "PHYSICLAW_HW_OUTPUT": str(REPO_ROOT / "artifacts"),
            "PHYSICLAW_HW_MANUAL_ILLUSTRATIONS": "1",
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert result.returncode == 0, (
        f"Reference renderer exited {result.returncode}\n"
        + result.stdout
        + result.stderr
    )
    return json.loads(result.stdout.splitlines()[-1])


def _illustration_probe() -> None:
    import hashlib
    import json

    from hardware.assembly import cache
    from hardware.assembly import travel_ranges as t
    from hardware.assembly.build_procedures import main as build
    from hardware.documents.manual import assets
    from hardware.scheme import OUTPUT_DIR, SVG_DIR

    assert REPO_ROOT != Path(__file__).resolve().parents[2]
    assert (t.X_TRAVEL, t.Y_TRAVEL) == t.ILLUSTRATION_TRAVEL
    assert (t.X_EXTRUSION_LENGTH, t.Y_EXTRUSION_LENGTH) == (190, 345)
    assert OUTPUT_DIR == REPO_ROOT / "artifacts/manual-illustrations"
    assert assets.SVG_DIR == SVG_DIR
    # Exercise the production worker/checkpoint recovery path: exact HLR can
    # segfault, and an unguarded in-process render makes this test flaky.
    result = build(
        [
            "--stems",
            "frame_10_extrusion_tnut",
            "--jobs",
            "1",
            "--log-format",
            "json",
        ]
    )
    assert result == 0
    print(
        json.dumps(
            {
                "source_key": cache.source_key("frame_10_extrusion_tnut"),
                "svgs": {
                    p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in SVG_DIR.glob("*.svg")
                },
            }
        )
    )


def _probe(x_delta: int, y_delta: int) -> None:
    import html
    import re
    from html.parser import HTMLParser
    from xml.etree import ElementTree

    from hardware.assembly import cache

    root = REPO_ROOT
    assert root != Path(__file__).resolve().parents[2], (
        "probe requires an isolated copy"
    )
    reference_before = _reference_probe()
    travel_source = root / "hardware/assembly/travel_ranges.py"
    stems = (
        "frame_10_extrusion_tnut",
        "linear_10_y",
        "linear_32_x",
        "phone_10_extrusion",
        "camera_40_frame",
    )
    before = {stem: cache.source_key(stem) for stem in stems}
    source = travel_source.read_text(encoding="utf-8")
    source_stat = travel_source.stat()
    py_compile.compile(
        str(travel_source),
        doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP,
    )
    configured_travel = []
    for axis, delta in zip(("X", "Y"), (x_delta, y_delta)):
        configured_travel.append(
            int(re.search(rf"^{axis}_TRAVEL = (\d+)", source, re.MULTILINE)[1]) + delta
        )
        source, count = re.subn(
            rf"^{axis}_TRAVEL = (\d+)",
            lambda match: f"{axis}_TRAVEL = {int(match[1]) + delta}",
            source,
            flags=re.MULTILINE,
        )
        assert count == 1
    travel_source.write_text(source, encoding="utf-8")
    # Equal-size, same-timestamp edits must affect actual imported CAD code too,
    # not merely its source hash. Leave a valid-looking stale .pyc behind.
    assert travel_source.stat().st_size == source_stat.st_size
    os.utime(travel_source, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
    # No module has imported travel yet; subsequent consumers see the changed build.
    assert all(cache.source_key(stem) != before[stem] for stem in stems)
    reference_after = _reference_probe()
    assert reference_before["svgs"] == reference_after["svgs"]
    assert reference_after["source_key"] != cache.source_key(stems[0])

    from hardware import check
    from hardware.assembly import travel_ranges as t
    from hardware.documents.bom.loader import load_bom_rows
    from hardware.documents.common import loc
    from hardware.documents.dimensions import resolve_dimensions
    from hardware.documents.drawing import extrusion as drawing
    from hardware.documents.manual import assets
    from hardware.documents.manual import build as manual
    from hardware.documents.manual.assets import ExternalAssets
    from hardware.documents.manual.pages import load_pages
    from hardware.documents.manual.paginate import (
        assign_page_numbers,
        paginate_bom_pages,
    )
    from hardware.documents.sourcing import build as sourcing

    rows = load_bom_rows()
    from hardware.scheme import OUTPUT_DIR

    assert (t.X_TRAVEL, t.Y_TRAVEL) == tuple(configured_travel)
    assert assets.SVG_DIR == OUTPUT_DIR / "manual-illustrations/svg"
    by_id = {row["part_id"]: row for row in rows}
    cuts = drawing.cut_list(rows)
    lengths = {
        "ext-2040-y": t.Y_EXTRUSION_LENGTH,
        "ext-2040-x": t.X_EXTRUSION_LENGTH,
        "ext-1020-x-beam": t.X_BEAM_LENGTH,
        "ext-1020-phone-support": t.PHONE_BED_BEAM_LENGTH,
        "rail-y": t.Y_RAIL_LENGTH,
        "rail-x": t.X_RAIL_LENGTH,
    }
    for part_id, length in lengths.items():
        for lang in ("en", "zh"):
            assert f"{length:g} mm" in by_id[part_id]["spec"][lang], (part_id, lang)
    assert check.check_extrusions(rows) == []
    assert check.check_sourcing(rows) == []

    pages = load_pages()
    by_page = {page["page"]: page for page in pages}
    assert next(page for page in pages if page["type"] == "bom")["rows"] == rows
    for page_id, index, part_id in (
        ("frame_10", 0, "ext-2040-y"),
        ("frame_10", 1, "ext-2040-x"),
        ("linear_30", 0, "ext-1020-x-beam"),
        ("phone_10", 0, "ext-1020-phone-support"),
        ("linear_10", 0, "rail-y"),
        ("linear_32", 0, "rail-x"),
    ):
        row = by_page[page_id]["bom"]["rows"][index]
        for lang in ("en", "zh"):
            assert f"{lengths[part_id]:g} mm" in loc(row["spec"], lang), (page_id, lang)
        assert row["qty"] == by_id[part_id]["qty"]
    paginate_bom_pages(pages)
    assign_page_numbers(pages)
    output = root / "artifacts"
    output.mkdir(exist_ok=True)

    class InquiryButtons(HTMLParser):
        def __init__(self, markup):
            super().__init__()
            self.messages = []
            self.feed(markup)

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "button" and "data-q" in attrs:
                self.messages.append(attrs["data-q"])

    for lang in ("en", "zh"):
        # Exercise the real text renderers. CAD-backed figures are checked through
        # source keys and STEP builds below; no placeholder figure files are emitted.
        manual_html = manual.render_document(
            pages, "", manual.Ctx(lang, ExternalAssets())
        )
        vendor_file = sourcing.VENDOR_FILES[lang]
        authored = sourcing.load_sourcing_data(vendor_file)
        vendor_bytes = vendor_file.read_bytes()
        entries = resolve_dimensions(authored, source=str(vendor_file))
        sourcing_html = sourcing.render_document(rows, authored, "", lang)
        drawing_html = drawing.render_document(cuts, lang)
        for name, document in (
            ("manual", manual_html),
            ("sourcing", sourcing_html),
            ("drawing", drawing_html),
        ):
            assert not re.search(r"\{[A-Z][A-Z0-9_]*\}", document), name
            (output / f"{name}-{lang}.html").write_text(document, encoding="utf-8")
        for length in lengths.values():
            assert f"{length:g}" in manual_html
            assert f"{length:g}" in sourcing_html
        for cut in cuts:
            assert f">{cut.spec.length:g} mm<" in drawing_html
            assert f">{cut.spec.length:g}<" in drawing_html
        # Measure the actual SVG faces at their declared scales, then compare
        # with the lengths checked in STEP below. Coordinates round to 0.001 mm
        # on paper, so allow only that rounding amplified by the view scale.
        sheet = ElementTree.fromstring(
            re.search(r"<svg\b.*?</svg>", drawing_html, re.S)[0]
        )
        faces = [
            node
            for node in sheet.iter("{http://www.w3.org/2000/svg}rect")
            if float(node.get("x", "-1")) == drawing.MARGIN + 66
        ]
        scales = [
            int(match[1])
            for node in sheet.iter("{http://www.w3.org/2000/svg}text")
            if (match := re.search(r" · 1:(\d+)$", node.text or ""))
            and int(match[1]) > 1
        ]
        assert len(faces) == len(scales) == len(cuts)
        for face, scale, cut in zip(faces, scales, cuts):
            tolerance = 0.0005 * scale + 1e-6
            assert (
                abs(float(face.get("width")) * scale - lengths[cut.spec.part_id])
                <= tolerance
            )
            assert (
                abs(
                    float(face.get("height")) * scale
                    - drawing.PROFILES[cut.spec.profile].size[0]
                )
                <= tolerance
            )
        inquiries = "\n".join(entry.get("inquiry", "") for entry in entries)
        for length in lengths.values():
            assert f"{length:g} mm" in inquiries
        copied_messages = InquiryButtons(sourcing_html).messages
        for entry in entries:
            if entry.get("inquiry") not in (None, "Ditto"):
                assert entry["inquiry"] in copied_messages
        for entry in entries:
            suppliers = entry.get("suppliers", [])
            if not isinstance(suppliers, list):
                continue
            for supplier in suppliers:
                for part in supplier.get("details", {}).get("parts", []):
                    assert part["part_number"] in html.unescape(sourcing_html)
        assert vendor_file.read_bytes() == vendor_bytes

    # Measure exported and re-imported STEP geometry, not just Python constants.
    from build123d import export_step, import_step

    from hardware.assembly.bom import collect
    from hardware.assembly.procedures import frame_10_extrusion_tnut as frame
    from hardware.assembly.procedures import phone_10_extrusion as phone
    from hardware.assembly.procedures.linear_10_y import LI10Y
    from hardware.assembly.procedures.linear_32_x import LI32X
    from hardware.parts.standard.extrusion import Extrusion1020, Extrusion2040

    assert (frame.LONG_LENGTH, frame.SHORT_LENGTH) == (
        t.Y_EXTRUSION_LENGTH,
        t.X_EXTRUSION_LENGTH,
    )
    assert phone.BEAM_LENGTH == t.PHONE_BED_BEAM_LENGTH

    def assert_step_length(shape, path, axis, expected):
        assert export_step(shape, str(path))
        restored = import_step(str(path))
        assert restored.is_valid
        assert abs(getattr(restored.bounding_box().size, axis) - expected) < 1e-5

    for cut in cuts:
        part_type = Extrusion2040 if cut.spec.profile == "2040" else Extrusion1020
        shape = part_type(length=cut.spec.length).build()
        path = output / f"{cut.spec.part_id}.step"
        assert_step_length(shape, path, "Z", cut.spec.length)

    fasteners = {}
    for cls, axis, page_id, copies in (
        (LI10Y, "y", "linear_10", 2),
        (LI32X, "x", "linear_32", 1),
    ):
        assembly = cls(exploded=False)
        parts = collect(assembly)
        screws = next(
            part.qty for part in parts if part.key == ("Screw", "BHCS", "M3", 8)
        )
        nuts = next(part.qty for part in parts if part.key == ("TNut", "hammer", "M3"))
        assert screws == nuts
        fasteners[axis] = copies * screws
        rail_rows = by_page[page_id]["bom"]["rows"]
        assert [int(row["qty"]) for row in rail_rows[1:]] == [copies * screws] * 2
        path = output / f"rail-{axis}.step"
        assert_step_length(assembly.build(), path, "X", lengths[f"rail-{axis}"])
    assert int(by_id["bhcs-m3x8"]["qty"]) == sum(fasteners.values()) + 8
    assert int(by_id["tnut-dropin-m3"]["qty"]) == sum(fasteners.values()) + 4
    assert not any(
        name == "src" or name.startswith("physiclaw") for name in sys.modules
    )


@pytest.mark.parametrize("x_delta,y_delta", [(20, 0), (0, 20), (25, 35)])
def test_changed_travel_keeps_documents_step_and_cache_consistent(
    tmp_path, x_delta, y_delta
):
    shutil.copytree(
        REPO_ROOT / "hardware",
        tmp_path / "hardware",
        ignore=shutil.ignore_patterns("output", "__pycache__"),
    )
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copyfile(REPO_ROOT / name, tmp_path / name)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), str(x_delta), str(y_delta)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=240,
    )
    assert result.returncode == 0, result.stdout + result.stderr


if __name__ == "__main__":
    if sys.argv[1:] == ["--illustrations"]:
        _illustration_probe()
    else:
        _probe(*map(int, sys.argv[1:]))
