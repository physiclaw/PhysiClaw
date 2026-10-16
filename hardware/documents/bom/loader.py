"""Authored purchasing list shared by the manual, sourcing guide and drawings.

This is separate from hardware.assembly.bom, which collects parts from CAD builds.
"""

import json
from collections import Counter
from pathlib import Path

from hardware.documents import BuildError

BOM_FILE = Path(__file__).resolve().parent / "bom.json"


def read_bom_rows() -> list:
    """Read the shared list; leave row validation to builders or the static gate."""
    try:
        rows = json.loads(BOM_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BuildError(f"Cannot read purchasing BOM {BOM_FILE}: {exc}") from exc
    if not isinstance(rows, list):
        raise BuildError(f"Purchasing BOM {BOM_FILE} must be a JSON array")
    return rows


def bom_row_findings(rows: list) -> list[str]:
    """Validate the join keys without assuming every authored row is well formed."""
    if not rows:
        return ["no purchasing BOM rows"]
    findings = []
    if malformed := sum(not isinstance(row, dict) for row in rows):
        findings.append(f"{malformed} malformed BOM row(s) (not an object)")
    ids = [row.get("part_id") for row in rows if isinstance(row, dict)]
    if missing := sum(not isinstance(part_id, str) or not part_id for part_id in ids):
        findings.append(
            f"{missing} BOM row(s) missing a part_id (non-empty string required)"
        )
    counts = Counter(part_id for part_id in ids if isinstance(part_id, str) and part_id)
    if duplicates := sorted(part_id for part_id, count in counts.items() if count > 1):
        findings.append(f"duplicate BOM part_id(s): {', '.join(duplicates)}")
    return findings


def load_bom_rows() -> list[dict]:
    """Return validated purchasing rows in their authored order."""
    rows = read_bom_rows()
    if findings := bom_row_findings(rows):
        raise BuildError("\n".join(findings))
    return rows
