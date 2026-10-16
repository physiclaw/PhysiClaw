import json

import pytest

from hardware.documents import BuildError
from hardware.documents.bom import loader as bom


@pytest.fixture
def bom_file(tmp_path, monkeypatch):
    path = tmp_path / "bom.json"
    monkeypatch.setattr(bom, "BOM_FILE", path)
    return path


# ── load_bom_rows ─────────────────────────────────────────────────────────────


def test_load_bom_rows_reads_shared_data_in_authored_order(bom_file):
    rows = [{"part_id": "p2", "qty": "4"}, {"part_id": "p1", "qty": "2"}]
    bom_file.write_text(json.dumps(rows))
    assert bom.load_bom_rows() == rows


def test_empty_bom_raises(bom_file):
    bom_file.write_text("[]")
    with pytest.raises(BuildError, match="no purchasing BOM rows"):
        bom.load_bom_rows()


@pytest.mark.parametrize("part_id", [None, "", [], 123])
def test_load_bom_rows_with_invalid_part_id_raises(bom_file, part_id):
    bom_file.write_text(json.dumps([{"part_id": part_id}]))
    with pytest.raises(BuildError, match="missing a part_id"):
        bom.load_bom_rows()


def test_load_bom_rows_with_duplicate_part_ids_raises(bom_file):
    bom_file.write_text('[{"part_id": "p1"}, {"part_id": "p1"}]')
    with pytest.raises(BuildError, match="duplicate BOM part_id"):
        bom.load_bom_rows()


@pytest.mark.parametrize("contents", ["{}", '"bom"', "null"])
def test_bom_requires_an_array(bom_file, contents):
    bom_file.write_text(contents)
    with pytest.raises(BuildError, match="must be a JSON array"):
        bom.load_bom_rows()


def test_missing_bom_reports_a_build_error(bom_file):
    with pytest.raises(BuildError, match="Cannot read purchasing BOM"):
        bom.load_bom_rows()


def test_invalid_json_reports_a_build_error(bom_file):
    bom_file.write_text("{broken")
    with pytest.raises(BuildError, match="Cannot read purchasing BOM"):
        bom.load_bom_rows()


def test_malformed_row_reports_a_build_error(bom_file):
    bom_file.write_text('["stray row"]')
    with pytest.raises(BuildError, match="malformed BOM row"):
        bom.load_bom_rows()
