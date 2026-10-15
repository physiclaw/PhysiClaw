from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("build123d", reason="CAD group not installed")

from hardware.parts import build_custom_parts as printing  # noqa: E402
from hardware.parts.export_custom import ALL_PARTS  # noqa: E402
from hardware.scheme import REPO_ROOT  # noqa: E402


def test_print_kit_matches_export_registry_and_manual_quantity():
    actual = {cls: qty for cls, qty, _, _ in printing.CUSTOM_PARTS}
    assert len(actual) == len(printing.CUSTOM_PARTS), "duplicate print designs"
    assert actual == {type(part): part.qty for part in ALL_PARTS}
    content = json.loads(
        (REPO_ROOT / "hardware/manual/content/11_bom.json").read_text()
    )

    def find(value):
        if isinstance(value, dict):
            if value.get("part_id") == "printed-parts":
                yield value
            for item in value.values():
                yield from find(item)
        elif isinstance(value, list):
            for item in value:
                yield from find(item)

    (row,) = find(content)
    assert sum(actual.values()) == int(row["qty"])


def test_failed_part_export_preserves_previous_print_package(tmp_path, monkeypatch):
    tmp_path = tmp_path / "print-output"
    tmp_path.mkdir()
    target = tmp_path / "parts.zip"
    target.write_bytes(b"previous complete package")
    monkeypatch.setattr(printing, "OUT_DIR", tmp_path)
    monkeypatch.setattr(printing, "ZIP_PATH", target)

    def fail(shape, path):
        Path(path).write_bytes(b"partial STEP export")
        raise RuntimeError("CAD export failed")

    monkeypatch.setattr(printing, "export_step", fail)
    with pytest.raises(RuntimeError, match="CAD export failed"):
        printing.build()
    assert target.read_bytes() == b"previous complete package"
    assert list(tmp_path.iterdir()) == [target]
