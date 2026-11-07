import json

import pytest

from hardware.documents import BuildError
from hardware.documents.dimensions import resolve_dimensions
from hardware.documents.sourcing import build as sourcing


def test_dimension_tokens_resolve_nested_data_without_mutating_templates():
    template = [{"spec": "{X_RAIL_LENGTH} mm", "note": "send {inquiry}"}]
    resolved = resolve_dimensions(template, source="example.json")
    assert resolved[0]["spec"] != template[0]["spec"]
    assert template[0]["spec"] == "{X_RAIL_LENGTH} mm"
    assert resolved[0]["note"] == "send {inquiry}"


def test_unknown_dimension_fails_with_source_and_token():
    with pytest.raises(BuildError, match=r"example.json.*\{X_RAIL_LENGHT\}"):
        resolve_dimensions({"spec": "{X_RAIL_LENGHT} mm"}, source="example.json")


def test_vendor_scaffold_preserves_dimension_tokens(tmp_path, monkeypatch):
    path = tmp_path / "global.json"
    entries = [{"part_id": "rail", "inquiry": "{X_RAIL_LENGTH} mm"}]
    path.write_text(json.dumps(entries), encoding="utf-8")
    monkeypatch.setattr(sourcing, "VENDOR_FILES", {"en": path})
    monkeypatch.setattr(sourcing, "load_bom_rows", lambda: [{"part_id": "rail"}])
    monkeypatch.setattr(sourcing, "render_document", lambda *args: "<html></html>")
    sourcing.build(["en"], tmp_path / "output", scaffold=True)
    assert json.loads(path.read_text(encoding="utf-8")) == entries
