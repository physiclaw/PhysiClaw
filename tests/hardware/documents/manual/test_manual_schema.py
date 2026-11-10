"""Reject authoring mistakes before either the static gate or renderer proceeds."""

import copy
import json

import pytest

from hardware.documents import BuildError
from hardware.documents.bom import loader as bom
from hardware.documents.manual import pages as manual_pages


@pytest.fixture(scope="module")
def authored_pages():
    # Start from real authored JSON, independently of the validator under test.
    pages = {}
    for path in sorted(manual_pages.CONTENT_DIR.glob("*.json")):
        for page in json.loads(path.read_text(encoding="utf-8")):
            pages.setdefault(page["type"], page)
    return pages


@pytest.mark.parametrize(
    ("kind", "path", "value"),
    [
        ("solo", ("figuers",), []),
        ("solo", ("gridStyle",), "gap:2mm"),
        ("solo", ("_pageno",), 1),
        ("opener", ("notes",), []),
        ("cover", ("head",), {}),
        ("cover", ("render", "alt"), "Ignored by the cover renderer"),
        ("solo", ("head", "titel"), "Typo"),
        ("solo", ("head", "small"), []),
        ("solo", ("head",), {"small": "Missing title"}),
        ("solo", ("figures", 0, "scr"), "part.svg"),
        ("solo", ("figures", 0, "style"), {}),
        ("solo", ("figures", 0, "alt"), {"en": "Not localized"}),
        ("solo", ("figures", 0, "alt"), None),
        ("solo", ("figures", 0, "src"), "part.png"),
        ("solo", ("figures", 0, "src"), "../part.svg"),
        ("solo", ("figures", 0, "src"), "folder\\part.svg"),
        ("solo", ("notes", 0, "styel"), "top:2mm"),
        ("solo", ("notes", 0, "classes"), []),
        ("solo", ("notes", 0, "classes"), "  "),
        ("solo", ("notes", 0, "style"), False),
        ("solo", ("notes", 0, "h3Style"), 0),
        ("solo", ("notes", 0, "icon"), []),
        ("solo", ("notes", 0, "icon"), ""),
        ("solo", ("notes", 0, "body"), {"zh": "没有英文"}),
        ("solo", ("notes", 0, "body"), {}),
        ("solo", ("notes", 0, "body"), {"en": "Text", "zn": "错别字"}),
        ("solo", ("notes", 0, "body"), {"en": "Text", "zh": False}),
        ("solo", ("bom", "styel"), "top:2mm"),
        ("solo", ("bom", "style"), []),
        ("solo", ("bom", "rows", 0, "quantity"), "1"),
        ("solo", ("bom", "rows", 0, "qty"), True),
        ("solo", ("bom", "rows", 0, "qty"), 1),
        ("solo", ("bom", "rows", 0, "qty"), ""),
        ("solo", ("labels",), [{"style": False, "text": "Label"}]),
        ("solo", ("labels",), [{"style": "", "text": "Label", "extra": 1}]),
        ("tall-left", ("gridStyle",), 0),
        ("tall-left", ("variant",), "heor"),
        ("tall-left", ("variant",), False),
        ("main-inset-br", ("notesInside",), "false"),
        ("main-inset-br", ("notesInside",), 1),
        ("main-inset-br", ("notesInside",), None),
        ("intro", ("links", 0, "logo"), "githbu"),
        ("intro", ("links", 0, "url"), False),
        ("intro", ("links", 0, "url"), ""),
        ("intro", ("links", 0, "href"), "https://example.com"),
        ("hardware-ref", ("entries", 0, "src"), 0),
        ("hardware-ref", ("entries", 0, "iconLabel"), "Hidden by src"),
        ("hardware-ref", ("entries", 0, "ref"), False),
        ("hardware-ref", ("entries", 0, "style"), "Ignored"),
        ("printed-parts", ("specs", 0, "key"), "Typo"),
        ("printed-parts", ("notesBefore", 0, "extra"), "Ignored"),
        ("printed-parts", ("notesAfter", 0, "extra"), "Ignored"),
        ("back", ("sub",), 0),
        ("back", ("sub",), None),
        ("toc", ("rows", 0, "_pgno"), 1),
        ("toc", ("rows", 0, "extra"), "Ignored"),
    ],
)
def test_invalid_authored_fields_fail_with_context(
    authored_pages, tmp_path, kind, path, value
):
    page = copy.deepcopy(authored_pages[kind])
    target = page
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    chapter = tmp_path / "chapter.json"
    chapter.write_text(json.dumps([page]), encoding="utf-8")

    with pytest.raises(BuildError) as error:
        manual_pages.load_chapter(chapter)

    field = "".join(f"[{key}]" if isinstance(key, int) else f".{key}" for key in path)
    assert f"chapter.json: page {page['page']!r}{field}" in str(error.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("quantity", "2"),
        ("cls", "Requires an English grouping key"),
        ("component", {"zh": "Requires English"}),
        ("spec", []),
        ("desc", {"en": "Use", "zz": "Typo"}),
        ("qty", 2),
        ("qty", None),
        ("break_before", "false"),
        ("break_before", 0),
    ],
)
def test_resolved_purchasing_rows_are_validated(tmp_path, monkeypatch, field, value):
    row = bom.read_bom_rows()[0]
    row[field] = value
    monkeypatch.setattr(bom, "BOM_FILE", tmp_path / "bom.json")
    bom.BOM_FILE.write_text(json.dumps([row]), encoding="utf-8")
    chapter = tmp_path / "chapter.json"
    chapter.write_text(
        '[{"type":"bom","page":"parts","rows_source":"bom"}]', encoding="utf-8"
    )

    with pytest.raises(
        BuildError, match=rf"chapter.json: page 'parts'\.rows\[0\]\.{field}"
    ):
        manual_pages.load_chapter(chapter)


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (
            '[{"type":"solo","page":"a","figures":[],"page":"b"}]',
            "duplicate JSON field 'page'",
        ),
        (
            '[{"type":"solo","page":"a","figures":[{"src":"a.svg","src":"b.svg"}]}]',
            "duplicate JSON field 'src'",
        ),
        *[
            (
                f'[{{"type":"solo","page":"a","figures":[],"extra":{value}}}]',
                f"invalid JSON constant {value}",
            )
            for value in ("NaN", "Infinity", "-Infinity")
        ],
    ],
)
def test_nonstandard_json_is_rejected(tmp_path, payload, reason):
    chapter = tmp_path / "chapter.json"
    chapter.write_text(payload, encoding="utf-8")
    with pytest.raises(BuildError) as error:
        manual_pages.load_chapter(chapter)
    assert f"chapter.json: {reason}" in str(error.value)


@pytest.mark.parametrize(
    ("page", "field", "reason"),
    [
        ({"type": "solo", "page": "first", "figures": []}, "page", "duplicate page id"),
        (
            {
                "type": "toc",
                "page": "toc",
                "rows": [{"page": "missing", "label": "Missing"}],
            },
            "rows[0].page",
            "unknown page id",
        ),
    ],
)
def test_load_pages_checks_references_across_chapters(
    tmp_path, monkeypatch, page, field, reason
):
    monkeypatch.setattr(manual_pages, "CONTENT_DIR", tmp_path)
    (tmp_path / "01_first.json").write_text(
        '[{"type":"solo","page":"first","figures":[]}]', encoding="utf-8"
    )
    (tmp_path / "02_second.json").write_text(json.dumps([page]), encoding="utf-8")
    with pytest.raises(BuildError) as error:
        manual_pages.load_pages()
    assert f"02_second.json: page {page['page']!r}.{field}: {reason}" in str(
        error.value
    )


def test_multiple_bom_tables_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(manual_pages, "CONTENT_DIR", tmp_path)
    pages = [
        {"type": "bom", "page": name, "rows_source": "bom"}
        for name in ("parts", "more-parts")
    ]
    (tmp_path / "chapter.json").write_text(json.dumps(pages), encoding="utf-8")
    with pytest.raises(BuildError, match="only one BOM table page"):
        manual_pages.load_pages()


@pytest.mark.parametrize(
    ("kind", "target", "reason"),
    [
        ("solo", "parts-2", "duplicate page id"),
        ("solo", "parts-3", None),
        ("toc", "parts-2", None),
        ("toc", "parts-3", "unknown page id"),
    ],
)
def test_static_and_build_loaders_agree_on_generated_bom_ids(
    tmp_path, monkeypatch, kind, target, reason
):
    from hardware import check
    from hardware.documents.manual.paginate import (
        assign_page_numbers,
        paginate_bom_pages,
    )

    rows = [
        {
            "part_id": f"part-{i}",
            "cls": {"en": "Hardware"},
            "component": {"en": "Part"},
            "spec": "",
            "qty": "1",
            "desc": "",
            "break_before": i == 1,
        }
        for i in range(2)
    ]
    monkeypatch.setattr(bom, "BOM_FILE", tmp_path / "bom.json")
    bom.BOM_FILE.write_text(json.dumps(rows), encoding="utf-8")
    content = tmp_path / "content"
    content.mkdir()
    monkeypatch.setattr(manual_pages, "CONTENT_DIR", content)
    monkeypatch.setattr(check, "CONTENT_DIR", content)
    page = (
        {"type": "solo", "page": target, "figures": []}
        if kind == "solo"
        else {
            "type": "toc",
            "page": "toc",
            "rows": [{"page": target, "label": "Parts continued"}],
        }
    )
    (content / "01_front.json").write_text(json.dumps([page]), encoding="utf-8")
    (content / "11_bom.json").write_text(
        '[{"type":"bom","page":"parts","rows_source":"bom"}]', encoding="utf-8"
    )

    findings, chapters = check.load_content()
    if reason:
        with pytest.raises(BuildError, match=reason) as error:
            manual_pages.load_pages()
        assert findings == [f"manual: {error.value}"]
        return

    pages = manual_pages.load_pages()
    assert findings == []
    assert pages == [page, {"type": "bom", "page": "parts", "rows": rows}]
    assert [item for chapter in chapters.values() for item in chapter] == pages
    paginate_bom_pages(pages)
    assign_page_numbers(pages)
    if kind == "toc":
        assert pages[0]["rows"][0]["_pgno"] == 3


def test_all_real_chapters_validate_and_render_in_both_languages():
    from hardware.documents.manual import build

    pages = manual_pages.load_pages()
    build.paginate_bom_pages(pages)
    build.assign_page_numbers(pages)
    for lang in ("en", "zh"):
        html = build.render_document(pages, "", build.Ctx(lang, build.ExternalAssets()))
        assert html.count('<section class="page') == len(pages)
