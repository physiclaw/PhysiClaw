import json

import pytest

from hardware.documents import BuildError
from hardware.documents.bom import loader as bom
from hardware.documents.manual import pages as manual_pages

# ── load_pages ────────────────────────────────────────────────────────────────


def test_load_pages_concatenates_content_files_in_filename_order(tmp_path, monkeypatch):
    monkeypatch.setattr(manual_pages, "CONTENT_DIR", tmp_path)
    front = {"type": "solo", "page": "front", "figures": []}
    later = {"type": "solo", "page": "a", "figures": []}
    (tmp_path / "01_a.json").write_text(json.dumps([later]), encoding="utf-8")
    (tmp_path / "00_front.json").write_text(json.dumps([front]), encoding="utf-8")

    assert manual_pages.load_pages() == [front, later]


def test_manual_bom_page_uses_shared_rows_without_changing_presentation(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(bom, "BOM_FILE", tmp_path / "parts.json")
    rows = [{"part_id": "rail-y", "qty": "2"}]
    bom.BOM_FILE.write_text(json.dumps(rows), encoding="utf-8")
    # Keep the data outside the chapter glob, as it is in the source tree.
    content = tmp_path / "content"
    content.mkdir()
    monkeypatch.setattr(manual_pages, "CONTENT_DIR", content)
    (content / "11_bom.json").write_text(
        json.dumps(
            [
                {
                    "type": "bom",
                    "page": "bom-list",
                    "head": {"title": "Parts"},
                    "rows_source": "bom",
                }
            ]
        ),
        encoding="utf-8",
    )
    assert manual_pages.load_pages() == [
        {"type": "bom", "page": "bom-list", "head": {"title": "Parts"}, "rows": rows}
    ]


@pytest.mark.parametrize(
    "page",
    [
        {"type": "bom", "rows": []},
        {"type": "bom", "rows_source": "bom", "rows": []},
        {"type": "bom", "rows_source": "suppliers"},
        {"type": "solo", "rows_source": "bom"},
    ],
)
def test_manual_rejects_duplicated_or_invalid_bom_sources(tmp_path, monkeypatch, page):
    monkeypatch.setattr(manual_pages, "CONTENT_DIR", tmp_path)
    (tmp_path / "11_bom.json").write_text(json.dumps([page]), encoding="utf-8")
    with pytest.raises(BuildError):
        manual_pages.load_pages()


@pytest.mark.parametrize("chapter", [{}, None, "chapter", [None], ["page"]])
def test_manual_rejects_malformed_chapter_data(tmp_path, monkeypatch, chapter):
    monkeypatch.setattr(manual_pages, "CONTENT_DIR", tmp_path)
    (tmp_path / "00_front.json").write_text(json.dumps(chapter), encoding="utf-8")
    with pytest.raises(
        BuildError, match="manual chapter must be a JSON array of page objects"
    ):
        manual_pages.load_pages()


@pytest.mark.parametrize(
    ("changes", "field", "reason"),
    [
        ({"type": "tall-lefft"}, "type", "unknown layout"),
        ({"type": ["solo"]}, "type", "unknown layout"),
        ({"page": ""}, "page", "nonempty page id"),
        ({"type": "main-inset-br"}, "main", "required field"),
        ({"type": "tall-left", "figures": []}, "figures", "at least one figure"),
        ({"figures": "figure.svg"}, "figures", "array of objects"),
        ({"figures": [{}]}, "figures[0].src", "required field"),
        ({"figures": [{"src": 42}]}, "figures[0].src", "nonempty filename"),
        ({"head": []}, "head", "expected an object"),
        ({"head": {"title": {"en": []}}}, "head.title", "language-to-text"),
        (
            {"notes": [{"classes": "note", "h3": "Tip", "body": {"en": None}}]},
            "notes[0].body",
            "language-to-text",
        ),
        *(
            (
                {
                    "type": "hardware-ref",
                    "entries": [{"h3": "Part", "body": "Text", "iconLabel": label}],
                },
                "entries[0].iconLabel",
                "expected text",
            )
            for label in (None, 42, {"en": "Part"})
        ),
        (
            {"notes": [{"classes": "note", "h3": "Tip"}]},
            "notes[0].body",
            "required field",
        ),
        (
            {
                "notes": [
                    {"classes": "note", "h3": "Tip", "body": "Text", "icon": "typo"}
                ]
            },
            "notes[0].icon",
            "unknown note icon",
        ),
    ],
)
def test_manual_layout_errors_identify_chapter_page_and_field(
    tmp_path, changes, field, reason
):
    page = {"type": "solo", "page": "install", "figures": [{"src": "part.svg"}]}
    page.update(changes)
    chapter = tmp_path / "01_frame.json"
    chapter.write_text(json.dumps([page]), encoding="utf-8")

    with pytest.raises(BuildError) as error:
        manual_pages.load_chapter(chapter)

    message = str(error.value)
    assert f"01_frame.json: page {page['page']!r}.{field}:" in message
    assert reason in message


@pytest.mark.parametrize("translation", ["", None])
def test_manual_validation_preserves_html_and_english_fallback(tmp_path, translation):
    from hardware.documents.common import loc

    page = {
        "type": "solo",
        "page": "install",
        "figures": [],
        "notes": [
            {
                "classes": "note",
                "h3": "Tip",
                "body": {"en": "<b>Text</b>", "zh": translation},
            }
        ],
    }
    chapter = tmp_path / "01_frame.json"
    chapter.write_text(json.dumps([page]), encoding="utf-8")

    loaded = manual_pages.load_chapter(chapter)

    assert loaded == [page]
    assert loc(loaded[0]["notes"][0]["body"], "zh") == "<b>Text</b>"


def test_validated_layouts_match_renderers():
    from hardware.documents.manual.build import RENDERERS
    from hardware.documents.manual.schema import LAYOUT_FIELDS

    assert set(LAYOUT_FIELDS) == set(RENDERERS)


def test_optional_null_header_and_inset_still_render(tmp_path):
    from hardware.documents.manual import build

    page = {
        "type": "main-inset-br",
        "page": "install",
        "main": {"src": "part.svg"},
        "inset": None,
        "head": None,
    }
    chapter = tmp_path / "01_frame.json"
    chapter.write_text(json.dumps([page]), encoding="utf-8")
    loaded = manual_pages.load_chapter(chapter)
    assert loaded == [page]
    build.assign_page_numbers(loaded)
    html = build.render_document(loaded, "", build.Ctx("en", build.ExternalAssets()))
    assert 'src="assets/part.svg"' in html
    assert 'class="fig inset"' not in html


@pytest.mark.parametrize(
    "head",
    [
        None,
        {},
        {"title": "Parts"},
        {"title": {"en": "Parts", "zh": None}},
        {"title": {"en": "Parts", "zh": ""}},
    ],
)
def test_valid_bom_headers_survive_pagination_and_rendering(tmp_path, head):
    from hardware.documents.manual import build

    chapter = tmp_path / "11_bom.json"
    chapter.write_text(
        json.dumps(
            [{"type": "bom", "page": "parts", "head": head, "rows_source": "bom"}]
        ),
        encoding="utf-8",
    )
    pages = manual_pages.load_chapter(chapter)

    build.paginate_bom_pages(pages)
    build.assign_page_numbers(pages)

    assert len(pages) > 1
    assert pages[0]["head"] == head
    for lang, title in (("en", "Parts (cont.)"), ("zh", "Parts（续）")):
        html = build.render_document(pages, "", build.Ctx(lang, build.ExternalAssets()))
        if head:
            assert title in html
        else:
            assert pages[1]["head"] == head
