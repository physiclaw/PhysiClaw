"""Validate authored page shapes before pagination or rendering.

Keep this module standard-library-only so hw-check can give authors the chapter,
page and field to fix without loading CAD or generating any documents. Text and
inline CSS remain authored presentation, including trusted HTML and translations.
"""

from hardware.documents import BuildError
from hardware.documents.assets import NOTE_ICONS

# Required top-level fields for each renderer. Optional overlays are shared below.
LAYOUT_FIELDS = {
    "cover": ("render", "title", "tag"),
    "toc": ("rows",),
    "intro": ("notes", "links"),
    "hardware-ref": ("entries",),
    "printed-parts": ("notesBefore", "specs", "notesAfter"),
    "opener": ("secnum", "h1", "lede"),
    "solo": ("figures",),
    "tall-left": ("figures",),
    "wide-top": ("figures",),
    "main-inset-br": ("main",),
    "back": ("stamp", "h2", "quote", "brand"),
    "bom": ("rows",),
}


def _require(value: object, fields: tuple[str, ...], where: str) -> dict:
    if not isinstance(value, dict):
        raise BuildError(f"{where}: expected an object")
    for field in fields:
        if field not in value:
            raise BuildError(f"{where}.{field}: required field is missing")
    return value


def _text_fields(value: dict, fields: tuple[str, ...], where: str) -> None:
    for field in fields:
        if field not in value:
            continue
        text = value[field]
        if isinstance(text, str):
            continue
        # English is the final fallback; it must resolve to text, never None.
        if isinstance(text, dict) and all(
            isinstance(translation, str) or (lang != "en" and translation is None)
            for lang, translation in text.items()
        ):
            continue
        raise BuildError(f"{where}.{field}: expected text or a language-to-text object")


def _items(value: dict, field: str, where: str) -> list[dict]:
    items = value.get(field, [])
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise BuildError(f"{where}.{field}: expected an array of objects")
    return items


def _figure(value: object, where: str) -> None:
    value = _require(value, ("src",), where)
    if not isinstance(value["src"], str) or not value["src"].strip():
        raise BuildError(f"{where}.src: expected a nonempty filename")


def validate_page(page: dict, *, source: str, number: int) -> None:
    """Check a loaded page, after resolving its shared purchasing rows."""
    where = f"{source}: page {page.get('page', f'#{number}')!r}"
    kind = page.get("type")
    if not isinstance(kind, str) or kind not in LAYOUT_FIELDS:
        raise BuildError(f"{where}.type: unknown layout {kind!r}")
    if not isinstance(page.get("page"), str) or not page["page"].strip():
        raise BuildError(f"{where}.page: expected a nonempty page id")
    _require(page, LAYOUT_FIELDS[kind], where)
    _text_fields(
        page,
        (
            "title",
            "tag",
            "secnum",
            "h1",
            "lede",
            "stamp",
            "h2",
            "quote",
            "brand",
        ),
        where,
    )

    if page.get("sub"):
        _text_fields(page, ("sub",), where)
    for field in ("render", "main"):
        if field in page:
            _figure(page[field], f"{where}.{field}")
    if page.get("inset") is not None:
        _figure(page["inset"], f"{where}.inset")
    figures = _items(page, "figures", where)
    if kind in ("tall-left", "wide-top") and not figures:
        raise BuildError(f"{where}.figures: this layout requires at least one figure")
    for i, figure in enumerate(figures):
        _figure(figure, f"{where}.figures[{i}]")

    if page.get("head") is not None:
        _require(page["head"], (), f"{where}.head")
        _text_fields(page["head"], ("title",), f"{where}.head")
    for field in ("notes", "notesBefore", "notesAfter"):
        for i, note in enumerate(_items(page, field, where)):
            at = f"{where}.{field}[{i}]"
            _require(note, ("classes", "h3", "body"), at)
            _text_fields(note, ("h3", "body"), at)
            if note.get("icon") and (
                not isinstance(note["icon"], str) or note["icon"] not in NOTE_ICONS
            ):
                raise BuildError(f"{at}.icon: unknown note icon {note['icon']!r}")
    for field, required, texts in (
        ("labels", ("style", "text"), ("text",)),
        ("links", ("logo", "word", "url"), ("word",)),
        ("specs", ("k", "v"), ("k", "v")),
        ("entries", ("h3", "body"), ("h3", "body")),
    ):
        for i, item in enumerate(_items(page, field, where)):
            at = f"{where}.{field}[{i}]"
            _require(item, required, at)
            _text_fields(item, texts, at)
            if field == "entries":
                if item.get("ref"):
                    _text_fields(item, ("ref",), at)
                if item.get("src"):
                    _figure(item, at)
                else:
                    _require(item, ("iconLabel",), at)
                    if not isinstance(item["iconLabel"], str):
                        raise BuildError(f"{at}.iconLabel: expected text")

    if "bom" in page:
        at = f"{where}.bom"
        _require(page["bom"], ("label", "rows"), at)
        _text_fields(page["bom"], ("label",), at)
        for i, row in enumerate(_items(page["bom"], "rows", at)):
            _require(row, ("component", "spec", "qty"), f"{at}.rows[{i}]")
            _text_fields(row, ("component", "spec"), f"{at}.rows[{i}]")
    if kind == "toc":
        for i, row in enumerate(_items(page, "rows", where)):
            at = f"{where}.rows[{i}]"
            _require(row, ("page", "label"), at)
            if not isinstance(row["page"], str) or not row["page"].strip():
                raise BuildError(f"{at}.page: expected a nonempty page id")
            _text_fields(row, ("label",), at)
