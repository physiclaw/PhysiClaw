"""Load the assembly manual's authored pages in chapter order."""

import json
from pathlib import Path

from hardware.documents import BuildError
from hardware.documents.bom import load_bom_rows

CONTENT_DIR = Path(__file__).resolve().parent / "content"


def load_chapter(path: Path) -> list[dict]:
    """Resolve purchasing rows while keeping chapter presentation in the manual."""
    chapter = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(chapter, list) or any(
        not isinstance(page, dict) for page in chapter
    ):
        raise BuildError(
            f"{path.name}: manual chapter must be a JSON array of page objects"
        )
    for page in chapter:
        if "rows_source" in page:
            if page.get("type") != "bom" or page["rows_source"] != "bom":
                raise BuildError(
                    f"{path.name}: rows_source is only supported as 'bom' on a BOM page"
                )
            if "rows" in page:
                raise BuildError(
                    f"{path.name}: use rows_source instead of duplicating BOM rows"
                )
            page.pop("rows_source")
            page["rows"] = load_bom_rows()
        elif page.get("type") == "bom":
            raise BuildError(
                f"{path.name}: BOM pages must reference the shared list with rows_source: bom"
            )
    return chapter


def load_pages() -> list[dict]:
    """Load chapters in filename order, resolving shared BOM references."""
    return [
        page
        for path in sorted(CONTENT_DIR.glob("*.json"))
        for page in load_chapter(path)
    ]
