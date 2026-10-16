"""Tests for hardware/check.py — the static cross-artifact consistency gate.

Each test builds (or mutates) a minimal fake world — procedures, patches,
manual content, sourcing data — and asserts which finding the gate reports.
"""

from __future__ import annotations

import json
import textwrap
from types import SimpleNamespace

import pytest

from hardware import check
from hardware.documents.bom import loader as bom
from hardware.documents.drawing.extrusion import SPECS

# One BOM row per extrusion the model cuts, stating its length in both
# languages, so the world's BOM satisfies the extrusion gate too.
EXTRUSION_ROWS = [
    {
        "part_id": cut.part_id,
        "spec": {"en": f"Length {cut.length:g} mm", "zh": f"长度 {cut.length:g} mm"},
        "qty": "2",
    }
    for cut in SPECS
]
EXTRUSION_IDS = [row["part_id"] for row in EXTRUSION_ROWS]

BASE_PROCEDURE = textwrap.dedent(
    """
    from hardware.assembly.base import ASSEMBLED_CAM0, BaseAssembly, EXPLODED_CAM0

    class FR10Base(BaseAssembly):
        views = [EXPLODED_CAM0, ASSEMBLED_CAM0]
    """
)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A consistent fake world: one procedure with a patch, manual content
    referencing its raw render, its snapshot, and a hand figure, and a
    BOM/sourcing pair that joins cleanly."""
    procedures = tmp_path / "procedures"
    patches = tmp_path / "patch"
    documents = tmp_path / "documents"
    manual = documents / "manual"
    vendors = documents / "sourcing" / "vendors"
    bom_dir = documents / "bom"
    content = manual / "content"
    for d in (procedures, patches, content, vendors, bom_dir):
        d.mkdir(parents=True)
    monkeypatch.setattr(check, "PROCEDURES_DIR", procedures)
    monkeypatch.setattr(check, "PATCH_DIR", patches)
    monkeypatch.setattr(check, "CONTENT_DIR", content)
    monkeypatch.setattr(bom, "BOM_FILE", bom_dir / "bom.json")
    monkeypatch.setattr(
        check,
        "VENDOR_FILES",
        {
            "zh": vendors / "cn.json",
            "en": vendors / "global.json",
        },
    )
    monkeypatch.setattr(check, "HAND_FIGURES", {"wire_splice.svg": "<svg/>"})

    (procedures / "frame_10_base.py").write_text(BASE_PROCEDURE)
    (patches / "frame_10_base_exploded_cam0.json").write_text(
        json.dumps([{"id": "abcd", "preop": "orig"}])
    )
    (content / "01_frame.json").write_text(
        json.dumps(
            [
                {
                    "figures": [
                        {"src": "frame_10_base_assembled_cam0.svg"},
                        {"src": "frame_10_base_exploded_cam0_abcd.svg"},
                        {"src": "wire_splice.svg"},
                    ]
                }
            ]
        )
    )
    write_bom(content, vendors, [{"part_id": "p1"}, {"part_id": "p2"}])
    return SimpleNamespace(
        procedures=procedures,
        patches=patches,
        vendors=vendors,
        content=content,
        bom=bom_dir / "bom.json",
    )


def write_bom(content, vendors, rows: list[dict], extrusions=EXTRUSION_ROWS) -> None:
    """A BOM page of ``rows`` plus the extrusion rows (the real ones unless
    a test hands in altered ones), and a vendor file that joins to every one
    of them."""
    rows = rows + extrusions
    (content / "11_bom.json").write_text(
        json.dumps([{"type": "bom", "rows_source": "bom"}])
    )
    (content.parent.parent / "bom" / "bom.json").write_text(json.dumps(rows))
    for filename in ("cn.json", "global.json"):
        (vendors / filename).write_text(
            json.dumps(
                [
                    {"part_id": r["part_id"]}
                    for r in rows
                    if isinstance(r, dict) and "part_id" in r
                ]
            )
        )


def run_check() -> list[str]:
    proc_findings, views_by_stem = check.check_procedures()
    patch_findings, leaves_by_svg = check.check_patches(views_by_stem)
    content_findings, pages_by_file = check.load_content()
    bom_findings, rows = check.load_purchasing_bom()
    return (
        proc_findings
        + patch_findings
        + content_findings
        + bom_findings
        + check.check_manual(views_by_stem, leaves_by_svg, pages_by_file)
        + check.check_sourcing(rows)
        + check.check_extrusions(rows)
    )


def assert_finding(fragment: str):
    findings = run_check()

    assert any(fragment in f for f in findings), findings


# ── clean world ───────────────────────────────────────────────────────────────


def test_consistent_world_reports_no_findings(world):
    assert run_check() == []


def test_main_on_consistent_world_returns_zero(world, capsys):
    assert (check.main([]), "SUCCESS" in capsys.readouterr().out) == (0, True)


def test_main_with_findings_returns_one(world, capsys):
    (world.procedures / "badname.py").write_text("")

    assert (check.main([]), "1 consistency findings" in capsys.readouterr().out) == (
        1,
        True,
    )


# ── procedures ────────────────────────────────────────────────────────────────


def test_misnamed_procedure_file_is_flagged(world):
    (world.procedures / "frame_clamp.py").write_text("")

    assert_finding("frame_clamp.py doesn't match")


def test_unknown_family_is_flagged(world):
    (world.procedures / "zzz_10_x.py").write_text("")

    assert_finding("zzz_10_x.py doesn't match")


def test_underscore_prefixed_file_is_exempt(world):
    (world.procedures / "_helpers.py").write_text("nonsense =")

    assert run_check() == []


def test_procedure_with_two_assembly_classes_is_flagged(world):
    (world.procedures / "frame_20_twin.py").write_text(
        "class A(BaseAssembly):\n    views = []\n"
        "class B(BaseAssembly):\n    views = []\n"
    )

    assert_finding("frame_20_twin.py defines 2 assembly classes")


def test_procedure_with_no_assembly_class_is_flagged(world):
    (world.procedures / "frame_20_none.py").write_text("class A:\n    pass\n")

    assert_finding("frame_20_none.py defines 0 assembly classes")


def test_duplicate_view_tags_are_flagged(world):
    (world.procedures / "frame_20_dup.py").write_text(
        "class A(BaseAssembly):\n    views = [EXPLODED_CAM0, EXPLODED_CAM0]\n"
    )

    assert_finding("frame_20_dup.py A: duplicate view tags")


def test_unknown_view_tag_is_flagged(world):
    (world.procedures / "frame_20_bad.py").write_text(
        'class A(BaseAssembly):\n    views = [("weird", 0)]\n'
    )

    assert_finding("unknown view tag ('weird', 0)")


def test_computed_views_expression_is_flagged(world):
    (world.procedures / "frame_20_dyn.py").write_text(
        "class A(BaseAssembly):\n    views = make_views()\n"
    )

    assert_finding("views is not a plain list literal")


def test_subclassing_procedure_inherits_parent_views(world):
    (world.procedures / "frame_20_child.py").write_text(
        "from hardware.assembly.procedures.frame_10_base import FR10Base\n"
        "class FR20Child(FR10Base):\n    pass\n"
    )
    (world.patches / "frame_20_child_assembled_cam0.json").write_text("[]")

    assert run_check() == []


def test_explicit_views_none_renders_all_and_is_permissive(world):
    (world.procedures / "frame_20_all.py").write_text(
        "class A(BaseAssembly):\n    views = None\n"
    )
    (world.patches / "frame_20_all_assembled_cam3.json").write_text("[]")

    assert run_check() == []


# ── patches ───────────────────────────────────────────────────────────────────


def test_patch_naming_unknown_procedure_is_flagged(world):
    (world.patches / "ghost_10_gone_exploded_cam0.json").write_text("[]")

    assert_finding("names unknown procedure 'ghost_10_gone'")


def test_patch_on_undeclared_view_is_flagged(world):
    (world.patches / "frame_10_base_exploded_cam1.json").write_text("[]")

    assert_finding("targets exploded_cam1, which frame_10_base doesn't declare")


def test_patch_with_malformed_name_is_flagged(world):
    (world.patches / "frame_10_base_cam0.json").write_text("[]")

    assert_finding("doesn't match <stem>_<variant>_cam<i>.json")


def test_patch_with_invalid_json_is_flagged(world):
    (world.patches / "frame_10_base_assembled_cam0.json").write_text("{nope")

    assert_finding("is not valid JSON")


def test_patch_with_non_array_body_is_flagged(world):
    (world.patches / "frame_10_base_assembled_cam0.json").write_text('{"id": "abcd"}')

    assert_finding("must be a JSON array of ops")


def test_patch_with_duplicate_op_ids_is_flagged(world):
    (world.patches / "frame_10_base_assembled_cam0.json").write_text(
        json.dumps([{"id": "abcd", "preop": "orig"}, {"id": "abcd", "preop": "orig"}])
    )

    assert_finding("duplicate op ids")


def test_patch_op_without_valid_id_is_flagged(world):
    (world.patches / "frame_10_base_assembled_cam0.json").write_text(
        json.dumps([{"id": "TOOLONGID", "preop": "orig"}])
    )

    assert_finding("op(s) without a valid id")


def test_patch_op_named_orig_is_flagged(world):
    # "orig" passes ID_RE (4 lowercase letters) but is the reserved
    # source-SVG sentinel — an op that adopts it is silently dropped at
    # replay, so the gate must reject it.
    (world.patches / "frame_10_base_assembled_cam0.json").write_text(
        json.dumps([{"id": "orig", "preop": "orig"}])
    )

    assert_finding("op(s) without a valid id")


def test_patch_op_with_unknown_preop_is_flagged(world):
    (world.patches / "frame_10_base_assembled_cam0.json").write_text(
        json.dumps([{"id": "abcd", "preop": "gone"}])
    )

    assert_finding("chains to unknown preop 'gone'")


def test_patch_preop_cycle_is_flagged(world):
    (world.patches / "frame_10_base_assembled_cam0.json").write_text(
        json.dumps([{"id": "abcd", "preop": "efgh"}, {"id": "efgh", "preop": "abcd"}])
    )

    assert_finding("preop chain loops")


def test_patch_op_missing_its_id_is_reported_not_crashed(world):
    (world.patches / "frame_10_base_assembled_cam0.json").write_text(
        json.dumps([{"preop": "orig"}])
    )

    assert_finding("op(s) without a valid id")


def test_patch_op_missing_its_preop_is_reported_not_crashed(world):
    (world.patches / "frame_10_base_assembled_cam0.json").write_text(
        json.dumps([{"id": "abcd"}])
    )

    assert_finding("chains to unknown preop None")


# ── manual content ────────────────────────────────────────────────────────────


def test_manual_reference_to_unknown_render_is_flagged(world):
    (world.content / "02_x.json").write_text(
        json.dumps([{"figures": [{"src": "ghost_10_gone_exploded_cam0.svg"}]}])
    )

    assert_finding("neither a procedure render nor a hand figure")


def test_manual_reference_to_undeclared_view_is_flagged(world):
    (world.content / "02_x.json").write_text(
        json.dumps([{"figures": [{"src": "frame_10_base_exploded_cam7.svg"}]}])
    )

    assert_finding("doesn't declare exploded_cam7 in views")


def test_manual_snapshot_without_patch_is_flagged(world):
    (world.content / "02_x.json").write_text(
        json.dumps([{"figures": [{"src": "frame_10_base_assembled_cam0_abcd.svg"}]}])
    )

    assert_finding("has no patch JSON")


def test_manual_snapshot_of_non_leaf_op_is_flagged(world):
    (world.patches / "frame_10_base_exploded_cam0.json").write_text(
        json.dumps([{"id": "abcd", "preop": "orig"}, {"id": "efgh", "preop": "abcd"}])
    )

    assert_finding("'abcd' is not a leaf op")


def test_manual_content_with_invalid_json_is_flagged(world):
    (world.content / "02_x.json").write_text("{nope")

    assert_finding("02_x.json is not valid JSON")


def test_malformed_manual_chapter_reports_a_finding(world):
    (world.content / "02_x.json").write_text("[null]")
    assert_finding("manual chapter must be a JSON array of page objects")


# ── sourcing ──────────────────────────────────────────────────────────────────


@pytest.fixture(params=["cn.json", "global.json"])
def vendor_filename(request):
    return request.param


def test_bom_row_without_part_id_is_flagged(world):
    write_bom(world.content, world.vendors, [{"part_id": "p1"}, {"spec": "x"}])

    assert_finding("1 BOM row(s) missing a part_id")


def test_duplicate_bom_part_ids_are_flagged(world):
    write_bom(world.content, world.vendors, [{"part_id": "p1"}, {"part_id": "p1"}])

    assert_finding("duplicate BOM part_id(s): p1")


def test_unhashable_bom_part_id_is_flagged_not_crashed(world):
    rows = json.loads(world.bom.read_text())
    rows.append({"part_id": []})
    world.bom.write_text(json.dumps(rows))
    assert_finding("missing a part_id (non-empty string required)")


def test_malformed_bom_row_is_flagged_not_crashed(world):
    # A non-object row must produce a finding, not an AttributeError that
    # aborts the whole gate and masks every other finding.
    write_bom(world.content, world.vendors, [{"part_id": "p1"}, "stray string"])

    assert_finding("malformed BOM row(s)")


def test_missing_shared_bom_is_flagged(world):
    world.bom.unlink()
    assert_finding("Cannot read purchasing BOM")


@pytest.mark.parametrize("contents", ["{broken", "{}"])
def test_unreadable_shared_bom_is_flagged(world, contents):
    world.bom.write_text(contents)
    assert any(finding.startswith("bom:") for finding in run_check())


def test_manual_cannot_override_shared_bom_rows(world):
    (world.content / "11_bom.json").write_text(
        json.dumps(
            [{"type": "bom", "rows_source": "bom", "rows": [{"part_id": "other"}]}]
        )
    )
    assert_finding("use rows_source instead of duplicating BOM rows")


def test_stale_vendor_part_id_is_flagged(world, vendor_filename):
    (world.vendors / vendor_filename).write_text(
        json.dumps([{"part_id": i} for i in ["p1", "p2", *EXTRUSION_IDS, "gone"]])
    )

    assert_finding("stale part_id(s) with no BOM row: gone")


def test_bom_row_missing_from_vendors_is_flagged(world, vendor_filename):
    (world.vendors / vendor_filename).write_text(
        json.dumps([{"part_id": i} for i in ["p1", *EXTRUSION_IDS]])
    )

    assert_finding(f"missing from {vendor_filename}: p2")


def test_missing_vendor_file_is_flagged(world, vendor_filename):
    (world.vendors / vendor_filename).unlink()

    assert_finding(f"{vendor_filename} not found")


def test_vendor_file_with_invalid_json_is_flagged(world, vendor_filename):
    (world.vendors / vendor_filename).write_text("{nope")

    assert_finding(f"{vendor_filename} is not valid JSON")


def test_duplicate_vendor_part_ids_are_flagged(world, vendor_filename):
    (world.vendors / vendor_filename).write_text(
        json.dumps([{"part_id": i} for i in ["p1", "p1", "p2", *EXTRUSION_IDS]])
    )

    assert_finding(f"duplicate part_id(s) in {vendor_filename}: p1")


# ── extrusions ────────────────────────────────────────────────────────────────


def test_bom_stating_another_extrusion_length_is_flagged(world):
    rows = [dict(r) for r in EXTRUSION_ROWS]
    rows[0]["spec"] = {"en": "Length 999 mm", "zh": "长度 999 mm"}
    write_bom(world.content, world.vendors, [{"part_id": "p1"}], extrusions=rows)

    assert_finding("extrusions: BOM row 'ext-2040-y' spec [en] does not state")


def test_extrusion_without_a_bom_row_is_flagged(world):
    rows = [r for r in EXTRUSION_ROWS if r["part_id"] != "ext-1020-x-beam"]
    write_bom(world.content, world.vendors, [{"part_id": "p1"}], extrusions=rows)

    assert_finding("extrusions: no BOM row with part_id 'ext-1020-x-beam'")
