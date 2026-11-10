"""Validate authored page shapes and references before rendering.

Keep this module standard-library-only so hw-check can give authors the chapter,
page and field to fix without loading CAD or generating any documents. Text and
inline CSS remain authored presentation, including trusted HTML and translations.
"""

import copy

from hardware.documents import BuildError
from hardware.documents.assets import NOTE_ICONS
from hardware.documents.common import HTML_LANG
from hardware.documents.manual.paginate import paginate_bom_pages

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

# Only fields consumed by each renderer are accepted. In particular, overlays
# on an opener or a gridStyle on a solo page would otherwise disappear silently.
_OVERLAYS = ("labels", "notes", "bom")
_LAYOUT_OPTIONAL = {
    "cover": (),
    "toc": ("head",),
    "intro": ("head",),
    "hardware-ref": ("head",),
    "printed-parts": ("head",),
    "opener": ("head",),
    "solo": ("head", *_OVERLAYS),
    "tall-left": ("head", "variant", "gridStyle", *_OVERLAYS),
    "wide-top": ("head", *_OVERLAYS),
    "main-inset-br": ("head", "inset", "notesInside", *_OVERLAYS),
    "back": ("sub",),
    "bom": ("head",),
}


def _require(
    value: object,
    fields: tuple[str, ...],
    where: str,
    optional: tuple[str, ...] = (),
) -> dict:
    if not isinstance(value, dict):
        raise BuildError(f"{where}: expected an object")
    for field in fields:
        if field not in value:
            raise BuildError(f"{where}.{field}: required field is missing")
    for field in value:
        if field not in fields and field not in optional:
            raise BuildError(f"{where}.{field}: unknown field")
    return value


def _text_fields(
    value: dict, fields: tuple[str, ...], where: str, *, localized: bool = False
) -> None:
    for field in fields:
        if field not in value:
            continue
        text = value[field]
        if isinstance(text, str) and not localized:
            continue
        at = f"{where}.{field}"
        if not isinstance(text, dict) or not all(
            isinstance(translation, str) or (lang != "en" and translation is None)
            for lang, translation in text.items()
        ):
            expected = (
                "a language-to-text object"
                if localized
                else "text or a language-to-text object"
            )
            raise BuildError(f"{at}: expected {expected}")
        # English is required as the final fallback. Only supported language
        # names are allowed, so misspellings cannot silently drop translations.
        _require(text, ("en",), at, tuple(HTML_LANG))


def _strings(
    value: dict, fields: tuple[str, ...], where: str, *, nonempty: bool = False
) -> None:
    for field in fields:
        if field in value and (
            not isinstance(value[field], str) or (nonempty and not value[field].strip())
        ):
            expected = "nonempty text" if nonempty else "text"
            raise BuildError(f"{where}.{field}: expected {expected}")


def _boolean(value: dict, field: str, where: str) -> None:
    if field in value and not isinstance(value[field], bool):
        raise BuildError(f"{where}.{field}: expected a boolean")


def _choice(value: dict, field: str, choices: tuple[str, ...], where: str) -> None:
    if field in value and value[field] not in choices:
        raise BuildError(
            f"{where}.{field}: unknown {field} {value[field]!r}; expected one of {choices}"
        )


def _items(value: dict, field: str, where: str) -> list[dict]:
    items = value.get(field, [])
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise BuildError(f"{where}.{field}: expected an array of objects")
    return items


def _source(value: dict, where: str) -> None:
    src = value["src"]
    if not isinstance(src, str) or not src.strip():
        raise BuildError(f"{where}.src: expected a nonempty filename")
    if not src.endswith(".svg") or "/" in src or "\\" in src:
        raise BuildError(f"{where}.src: expected an SVG filename without a directory")


def _figure(value: object, where: str, *, cover: bool = False) -> None:
    value = _require(value, ("src",), where, () if cover else ("style", "alt"))
    _source(value, where)
    _strings(value, ("style", "alt"), where)


def _purchasing_rows(page: dict, where: str) -> None:
    for i, row in enumerate(_items(page, "rows", where)):
        at = f"{where}.rows[{i}]"
        _require(
            row,
            ("part_id", "cls", "component", "spec", "qty", "desc"),
            at,
            ("break_before",),
        )
        _strings(row, ("part_id", "qty"), at, nonempty=True)
        # Grouping and pagination access the English keys directly.
        _text_fields(row, ("cls", "component"), at, localized=True)
        _text_fields(row, ("spec", "desc"), at)
        _boolean(row, "break_before", at)


def validate_page(page: dict, *, source: str, number: int) -> None:
    """Check a loaded page, after resolving its shared purchasing rows."""
    where = f"{source}: page {page.get('page', f'#{number}')!r}"
    kind = page.get("type")
    if not isinstance(kind, str) or kind not in LAYOUT_FIELDS:
        raise BuildError(f"{where}.type: unknown layout {kind!r}")
    if not isinstance(page.get("page"), str) or not page["page"].strip():
        raise BuildError(f"{where}.page: expected a nonempty page id")
    _require(
        page, ("type", "page", *LAYOUT_FIELDS[kind]), where, _LAYOUT_OPTIONAL[kind]
    )
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
            "sub",
        ),
        where,
    )

    _strings(page, ("gridStyle",), where)
    _choice(page, "variant", ("hero",), where)
    _boolean(page, "notesInside", where)
    for field in ("render", "main"):
        if field in page:
            _figure(page[field], f"{where}.{field}", cover=field == "render")
    if page.get("inset") is not None:
        _figure(page["inset"], f"{where}.inset")
    figures = _items(page, "figures", where)
    if kind in ("tall-left", "wide-top") and not figures:
        raise BuildError(f"{where}.figures: this layout requires at least one figure")
    for i, figure in enumerate(figures):
        _figure(figure, f"{where}.figures[{i}]")

    if (head := page.get("head")) is not None:
        at = f"{where}.head"
        _require(head, (), at, ("title", "small"))
        _text_fields(head, ("title",), at)
        _strings(head, ("small",), at)
        if "small" in head and "title" not in head:
            raise BuildError(f"{at}.title: required when small is provided")
    for field in ("notes", "notesBefore", "notesAfter"):
        for i, note in enumerate(_items(page, field, where)):
            at = f"{where}.{field}[{i}]"
            _require(note, ("classes", "h3", "body"), at, ("style", "h3Style", "icon"))
            _text_fields(note, ("h3", "body"), at)
            _strings(note, ("classes",), at, nonempty=True)
            _strings(note, ("style", "h3Style"), at)
            _choice(note, "icon", tuple(NOTE_ICONS), at)
    for field, required, texts, optional in (
        ("labels", ("style", "text"), ("text",), ()),
        ("links", ("logo", "word", "url"), ("word",), ()),
        ("specs", ("k", "v"), ("k", "v"), ()),
        ("entries", ("h3", "body"), ("h3", "body", "ref"), ("ref", "src", "iconLabel")),
    ):
        for i, item in enumerate(_items(page, field, where)):
            at = f"{where}.{field}[{i}]"
            _require(item, required, at, optional)
            _text_fields(item, texts, at)
            if field == "labels":
                _strings(item, ("style",), at)
            elif field == "links":
                _choice(item, "logo", ("github", "crab"), at)
                _strings(item, ("url",), at, nonempty=True)
            elif field == "entries":
                if "src" in item:
                    _source(item, at)
                    if "iconLabel" in item:
                        raise BuildError(
                            f"{at}.iconLabel: use either src or iconLabel, not both"
                        )
                elif "iconLabel" not in item:
                    raise BuildError(f"{at}.iconLabel: required when src is absent")
                else:
                    _strings(item, ("iconLabel",), at)

    if "bom" in page:
        at = f"{where}.bom"
        _require(page["bom"], ("label", "rows"), at, ("style",))
        _text_fields(page["bom"], ("label",), at)
        _strings(page["bom"], ("style",), at)
        for i, row in enumerate(_items(page["bom"], "rows", at)):
            row_at = f"{at}.rows[{i}]"
            _require(row, ("component", "spec", "qty"), row_at)
            _text_fields(row, ("component", "spec"), row_at)
            _strings(row, ("qty",), row_at, nonempty=True)
    if kind == "bom":
        _purchasing_rows(page, where)
    if kind == "toc":
        for i, row in enumerate(_items(page, "rows", where)):
            at = f"{where}.rows[{i}]"
            _require(row, ("page", "label"), at)
            if not isinstance(row["page"], str) or not row["page"].strip():
                raise BuildError(f"{at}.page: expected a nonempty page id")
            _text_fields(row, ("label",), at)


def validate_page_links(chapters: dict[str, list[dict]]) -> None:
    """Check document-wide references before rendering, also used by hw-check."""
    ids: dict[str, str] = {}
    bom_source = None
    bom_pages = []
    for source, pages in chapters.items():
        for page in pages:
            where = f"{source}: page {page['page']!r}"
            if page["page"] in ids:
                raise BuildError(
                    f"{where}.page: duplicate page id; first defined in {ids[page['page']]}"
                )
            ids[page["page"]] = source
            if page["type"] == "bom":
                if bom_source is not None:
                    raise BuildError(
                        f"{where}.type: only one BOM table page is supported; first defined in {bom_source}"
                    )
                bom_source = source
                bom_pages = [copy.deepcopy(page)]
    # Reserve exactly the continuation IDs the renderer will generate, without
    # changing the authored rows or headings returned by the loader.
    if bom_source is not None:
        paginate_bom_pages(bom_pages)
        for page in bom_pages[1:]:
            pid = page["page"]
            if pid in ids:
                raise BuildError(
                    f"{ids[pid]}: page {pid!r}.page: duplicate page id; "
                    f"generated by the BOM table in {bom_source}"
                )
            ids[pid] = bom_source
    for source, pages in chapters.items():
        for page in pages:
            if page["type"] != "toc":
                continue
            for i, row in enumerate(page["rows"]):
                if row["page"] not in ids:
                    raise BuildError(
                        f"{source}: page {page['page']!r}.rows[{i}].page: unknown page id {row['page']!r}"
                    )
