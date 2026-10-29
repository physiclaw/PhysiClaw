import json

import pytest

from hardware.documents import BuildError
from hardware.documents.bom import loader as bom
from hardware.documents.manual import pages as manual_pages

# ── load_pages ────────────────────────────────────────────────────────────────


def test_load_pages_concatenates_content_files_in_filename_order(tmp_path, monkeypatch):
    monkeypatch.setattr(manual_pages, "CONTENT_DIR", tmp_path)
    (tmp_path / "01_a.json").write_text('[{"page": "a"}]', encoding="utf-8")
    (tmp_path / "00_front.json").write_text('[{"page": "front"}]', encoding="utf-8")

    assert manual_pages.load_pages() == [{"page": "front"}, {"page": "a"}]


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
