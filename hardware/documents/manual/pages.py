"""Load the assembly manual's authored pages in chapter order."""

import json
from pathlib import Path

from hardware.documents import BuildError
from hardware.documents.bom.loader import load_bom_rows
from hardware.documents.dimensions import resolve_dimensions
from hardware.documents.manual.schema import validate_page, validate_page_links
from hardware.documents.manual.style_checks import inline_style_unit_errors

CONTENT_DIR = Path(__file__).resolve().parent / "content"


def load_chapter(path: Path) -> list[dict]:
    """Load a chapter, resolve purchasing rows, and validate its schema and styles."""

    def object_pairs(pairs: list[tuple[str, object]]) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                raise BuildError(f"{path.name}: duplicate JSON field {key!r}")
            result[key] = value
        return result

    def invalid_constant(value: str) -> None:
        raise BuildError(f"{path.name}: invalid JSON constant {value}")

    chapter = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=object_pairs,
        parse_constant=invalid_constant,
    )
    if not isinstance(chapter, list) or any(
        not isinstance(page, dict) for page in chapter
    ):
        raise BuildError(
            f"{path.name}: manual chapter must be a JSON array of page objects"
        )
    chapter = resolve_dimensions(chapter, source=str(path))
    for number, page in enumerate(chapter, start=1):
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
        validate_page(page, source=path.name, number=number)
    errors = [f"{path.name}: {error}" for error in inline_style_unit_errors(chapter)]
    if errors:
        raise BuildError("\n".join(errors))
    return chapter


def load_pages() -> list[dict]:
    """Load validated chapters in filename order and check page references."""
    chapters = {
        path.name: load_chapter(path) for path in sorted(CONTENT_DIR.glob("*.json"))
    }
    validate_page_links(chapters)
    return [page for chapter in chapters.values() for page in chapter]
