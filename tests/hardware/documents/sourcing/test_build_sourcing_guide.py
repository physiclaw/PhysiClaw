"""Tests for hardware/documents/sourcing/build.py — the pure data logic."""

from __future__ import annotations

import json
import re
from copy import deepcopy

import pytest

from hardware.documents import BuildError
from hardware.documents.sourcing import build as bsg


def bom_row(pid: str) -> dict:
    return {
        "part_id": pid,
        "component": {"en": pid},
        "spec": {"en": "spec"},
        "cls": {"en": "Frame"},
    }


# ── sync_entries ──────────────────────────────────────────────────────────────


def test_sync_entries_adds_bare_entries_for_missing_rows():
    rows = [bom_row("p1"), bom_row("p2")]

    synced, added, stale = bsg.sync_entries(rows, [{"part_id": "p1", "ref": "¥5"}])

    assert (synced, added, stale) == (
        [{"part_id": "p1", "ref": "¥5"}, {"part_id": "p2"}],
        ["p2"],
        [],
    )


def test_sync_entries_reorders_authored_entries_to_bom_order():
    rows = [bom_row("p1"), bom_row("p2")]
    entries = [{"part_id": "p2", "ref": "b"}, {"part_id": "p1", "ref": "a"}]

    synced, _, _ = bsg.sync_entries(rows, entries)

    assert [e["part_id"] for e in synced] == ["p1", "p2"]


def test_sync_entries_drops_and_reports_stale_ids():
    synced, _, stale = bsg.sync_entries([bom_row("p1")], [{"part_id": "gone"}])

    assert (synced, stale) == ([{"part_id": "p1"}], ["gone"])


def test_sync_entries_with_duplicate_entry_ids_raises():
    entries = [{"part_id": "p1"}, {"part_id": "p1"}]

    with pytest.raises(BuildError, match="duplicate part_id"):
        bsg.sync_entries([bom_row("p1")], entries)


# ── ditto_walk / supplier_columns ─────────────────────────────────────────────


def test_ditto_walk_merges_consecutive_dittos_into_the_anchor_span():
    spans, resolved = bsg.ditto_walk(
        ["¥5", "Ditto", "Ditto", "¥9"], "ref", ["a", "b", "c", "d"]
    )

    assert (spans, resolved) == ([3, 0, 0, 1], ["¥5", "¥5", "¥5", "¥9"])


def test_ditto_walk_on_the_first_row_raises_with_the_entry_id():
    with pytest.raises(BuildError, match="'ref' of entry 'p1'"):
        bsg.ditto_walk(["Ditto"], "ref", ["p1"])


def test_supplier_columns_expands_a_whole_row_ditto_to_every_slot():
    columns = bsg.supplier_columns([{"suppliers": "Ditto"}])

    assert [col[0] for col in columns] == ["Ditto", "Ditto", "Ditto"]


def test_supplier_columns_pads_short_slots_with_none():
    columns = bsg.supplier_columns([{"suppliers": [{"name": "shop"}]}])

    assert [col[0] for col in columns] == [{"name": "shop"}, None, None]


# ── clean_supplier_url ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://item.taobao.com/item.htm?abbucket=14&id=12345&mi_id=xyz"
            "&skuId=67890&spm=a21xtw.123&xxc=ad",
            "https://item.taobao.com/item.htm?id=12345&skuId=67890",
        ),
        (
            "https://item.taobao.com/item.htm?ns=1&id=98765",
            "https://item.taobao.com/item.htm?id=98765",
        ),
        ("https://shop123.taobao.com/", "https://shop123.taobao.com/"),
        (
            "https://example.com/listing?id=1&color=red",
            "https://example.com/listing?id=1&color=red",
        ),
    ],
    ids=["strips-tracking", "keeps-id-only", "shop-homepage", "non-taobao"],
)
def test_clean_supplier_url_keeps_only_identifying_params(url, expected):
    assert bsg.clean_supplier_url(url) == expected


@pytest.mark.parametrize(
    "domain",
    ["aliexpress.us", "aliexpress.com", "www.aliexpress.us", "www.aliexpress.com"],
)
def test_clean_aliexpress_item_url_removes_query_and_fragment(domain):
    base = f"https://{domain}/item/3256809048539619.html"
    assert (
        bsg.clean_supplier_url(
            base + "?spm=tracking&sku_id=123&channel=twinner#details"
        )
        == base
    )
    assert bsg.clean_supplier_url(base) == base


@pytest.mark.parametrize(
    "url",
    [
        "https://www.aliexpress.us/store/123?sort=price",
        "https://notaliexpress.us/item/123.html?sku_id=456",
        "https://www.aliexpress.us.example.com/item/123.html?sku_id=456",
    ],
)
def test_clean_supplier_url_leaves_other_links_unchanged(url):
    assert bsg.clean_supplier_url(url) == url


# ── accessories ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("lang", ["en", "zh"])
def test_accessories_stay_in_notes_without_changing_bom_or_checklist(lang):
    rows = [dict(bom_row("tip"), qty="1", desc="")]
    accessory = {
        "name": "Adapter <M3 & M5>",
        "url": "https://www.aliexpress.us/item/123.html?spm=tracking#details",
    }
    entries = [
        {
            "part_id": "tip",
            "note": "Required.",
            "accessories": [accessory],
        }
    ]
    table = bsg.render_table(rows, entries, lang)
    assert table.count("data-pid=") == 1
    assert table.count('type="checkbox"') == 1
    assert table.count('class="note-accessory"') == 1
    assert "Also buy" not in table
    assert "另需购买" not in table
    assert "Required." in table
    assert "Adapter &lt;M3 &amp; M5&gt;" in table
    assert 'href="https://www.aliexpress.us/item/123.html"' in table
    entries.append({"part_id": "other", "note": "Ditto"})
    rows.append(dict(bom_row("other"), qty="1", desc=""))
    shared = bsg.render_table(rows, entries, lang)
    assert shared.count('class="note-accessory"') == 1
    assert 'class="note" rowspan="2"' in shared


@pytest.mark.parametrize("note", [None, ""])
def test_accessories_render_without_note_text(note):
    rows = [dict(bom_row("tip"), qty="1", desc="")]
    entry = {
        "part_id": "tip",
        "accessories": [{"name": "Adapter", "url": "https://example.com/adapter"}],
    }
    if note is not None:
        entry["note"] = note
    table = bsg.render_table(rows, [entry], "en")
    assert 'class="note-accessory"' in table
    assert 'href="https://example.com/adapter"' in table


def test_accessory_vendors_each_show_link_above_localized_spec():
    accessories = [
        {"name": "Vendor 1", "url": "https://example.com/1", "spec": "M3-M5 <5pcs>"},
        {"name": "Vendor 2", "url": "https://example.com/2", "spec": "M3-M5 <5pcs>"},
        {
            "name": "Vendor 3",
            "url": "https://example.com/3",
            "spec": {"en": "10pcs", "zh": "10个"},
        },
    ]
    rendered = bsg.render_accessories(accessories, "en")
    assert rendered.count('class="accessory-spec">M3-M5 &lt;5pcs&gt;</div>') == 2
    assert rendered.count('class="note-accessory"') == 1
    assert rendered.count('class="accessory-offer"') == 3
    assert rendered.count("<a href=") == 3
    assert 'Vendor 1</a><div class="accessory-spec">' in rendered
    assert 'class="accessory-spec">10pcs</div>' in rendered
    assert 'class="accessory-spec">10个</div>' in bsg.render_accessories(
        accessories, "zh"
    )


# ── optional tools ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("lang", ["en", "zh"])
@pytest.mark.parametrize("count", [0, 1, 2])
def test_optional_tools_use_flexible_regional_links_outside_checklist(lang, count):
    sources = [
        {
            "name": f"Shop <{i}>",
            "url": f"https://www.aliexpress.us/item/{i}.html?spm=tracking",
            "product": {"en": "Small", "zh": "小号"},
        }
        for i in range(count)
    ]
    tool = {
        "name": {"en": "Hex key", "zh": "内六角扳手"},
        "spec": '3/16"',
        "purpose": {"en": "Camera <mount>", "zh": "相机<支架>"},
        "sources": {
            lang: sources + [{"name": "", "url": ""}],
            "zh" if lang == "en" else "en": [
                {"name": "Other region", "url": "https://example.com/other"}
            ],
        },
    }

    section = bsg.render_optional_tools([tool], lang)

    assert section.count("<a href=") == count
    assert "Other region" not in section
    assert "data-pid=" not in section
    assert 'type="checkbox"' not in section
    assert ("Hex key" if lang == "en" else "内六角扳手") in section
    assert ("Camera &lt;mount&gt;" if lang == "en" else "相机&lt;支架&gt;") in section
    if count:
        assert bsg.ui("pending", lang) not in section
        assert "Shop &lt;0&gt;</a>" in section
        assert 'href="https://www.aliexpress.us/item/0.html"' in section
        assert ("Small" if lang == "en" else "小号") in section
    else:
        assert f'<td class="offer pending">{bsg.ui("pending", lang)}</td>' in section


def test_empty_optional_tools_omit_the_section():
    assert bsg.render_optional_tools([], "en") == ""


@pytest.mark.parametrize("langs", [["en"], ["zh"], ["en", "zh"]])
@pytest.mark.parametrize("scaffold", [False, True])
def test_build_uses_and_scaffolds_only_selected_vendor_files(
    tmp_path, monkeypatch, langs, scaffold
):
    files = {
        "en": tmp_path / "global.json",
        "zh": tmp_path / "cn.json",
    }
    originals = {}
    for lang, path in files.items():
        path.write_text(
            json.dumps([{"part_id": "p1", "suppliers": [{"name": f"shop-{lang}"}]}])
        )
        originals[lang] = path.read_bytes()
    monkeypatch.setattr(bsg, "VENDOR_FILES", files)
    monkeypatch.setattr(bsg, "load_bom_rows", lambda: [bom_row("p1"), bom_row("p2")])

    def render(rows, entries, css, lang):
        assert entries[0]["suppliers"][0]["name"] == f"shop-{lang}"
        assert entries[1] == {"part_id": "p2"}
        return lang

    monkeypatch.setattr(bsg, "render_document", render)
    output = tmp_path / "output"
    written = bsg.build(langs, output, scaffold=scaffold)
    assert {p.name for p in written} == {bsg.LANG_FILENAME[lang] for lang in langs}
    for lang, path in files.items():
        if scaffold and lang in langs:
            assert json.loads(path.read_text())[-1] == {"part_id": "p2"}
        else:
            assert path.read_bytes() == originals[lang]


@pytest.fixture
def ordering_supplier():
    return {
        "name": "Example shop",
        "url": "https://example.com/",
        "details": {
            "label": "Ordering details",
            "title": "Parts & quantities",
            "heading": "Get a quote",
            "instructions": "Enter the parts below.",
            "parts": [
                {"part_number": "PART-123", "qty": 2, "description": "Cut to length"}
            ],
        },
    }


@pytest.mark.parametrize("ditto", ["Ditto", ["Ditto"]])
def test_supplier_details_share_one_dialog_across_ditto_rows(ordering_supplier, ditto):
    rows = [dict(bom_row(pid), qty="1", desc="") for pid in ("p1", "p2")]
    entries = [
        {
            "part_id": "p1",
            "suppliers": [ordering_supplier],
        },
        {"part_id": "p2", "suppliers": ditto},
    ]
    document = bsg.render_document(rows, entries, "", "en")
    assert document.count('<dialog class="supplier-guide"') == 1
    assert document.count('data-guide="supplier-guide-0-0"') == 1
    assert 'id="supplier-guide-0-0"' in document
    assert 'aria-label="Parts &amp; quantities"' in document
    assert 'class="guide-close"' in document
    assert 'aria-label="Close"' in document
    assert 'aria-hidden="true">×</span>' in document
    assert '<td class="offer" rowspan="2">' in document
    assert document.index("</table></div>") < document.index("<dialog")


def test_supplier_without_details_has_no_dialog_action():
    cell = bsg.render_supplier_cell({"name": "Example"}, "en", 1, "guide")
    assert "guide-open" not in cell
    assert (
        bsg.render_supplier_guides([{"suppliers": [{"name": "Example"}]}], "en") == ""
    )


def test_supplier_steps_render_settings_without_a_parts_table():
    details = {
        "heading": "Order parts",
        "steps": [
            {
                "title": {"en": "Set <options>", "zh": "设置"},
                "text": "Click <strong>Batch Edit</strong>.",
                "settings": [{"label": "Material & color", "value": "PA12 <gray>"}],
            }
        ],
    }
    rendered = bsg.render_supplier_guide_body(details, "en")
    assert '<ol class="guide-steps">' in rendered
    assert "Set &lt;options&gt;" in rendered
    assert '<th scope="row">Material &amp; color</th>' in rendered
    assert "PA12 &lt;gray&gt;" in rendered
    assert "Click <strong>Batch Edit</strong>." in rendered
    assert "guide-parts" not in rendered
    assert "设置" in bsg.render_supplier_guide_body(details, "zh")


@pytest.mark.parametrize("guide_lang", ["en", "zh"])
@pytest.mark.parametrize("ditto", ["Ditto", ["Ditto", "Ditto"]])
@pytest.mark.parametrize(
    "change", ["remove_details", "remove_supplier", "replace_supplier"]
)
def test_rebuild_follows_supplier_changes(
    tmp_path, monkeypatch, ordering_supplier, guide_lang, ditto, change
):
    rows = [dict(bom_row(pid), qty="1", desc="") for pid in ("p1", "p2")]
    suppliers = [ordering_supplier, {"name": "Other shop"}]
    entries = [
        {
            "part_id": "p1",
            "suppliers": suppliers,
            "inquiry": "Quote the parts",
            "note": "Ask the shop: {inquiry}",
        },
        {"part_id": "p2", "suppliers": ditto, "inquiry": "Ditto", "note": "Ditto"},
    ]
    files = {lang: tmp_path / f"vendors.{lang}.json" for lang in ("en", "zh")}
    other_lang = "zh" if guide_lang == "en" else "en"
    files[guide_lang].write_text(json.dumps(entries))
    files[other_lang].write_text(
        json.dumps([{"part_id": row["part_id"]} for row in rows])
    )
    monkeypatch.setattr(bsg, "VENDOR_FILES", files)
    monkeypatch.setattr(bsg, "load_bom_rows", lambda: rows)
    output = tmp_path / "output"
    bsg.build(["en", "zh"], output, scaffold=False)
    path = output / bsg.LANG_FILENAME[guide_lang]
    before = path.read_text()
    other_before = (output / bsg.LANG_FILENAME[other_lang]).read_bytes()
    assert before.count('<dialog class="supplier-guide"') == 1
    assert 'data-q="PART-123"' in before
    assert b"<dialog" not in other_before

    if change == "remove_details":
        suppliers[0].pop("details")
    elif change == "remove_supplier":
        suppliers.pop(0)
    else:
        replacement = deepcopy(ordering_supplier)
        replacement["name"] = "Replacement shop"
        replacement["details"]["parts"][0]["part_number"] = "NEW-456"
        suppliers[0] = replacement
    files[guide_lang].write_text(json.dumps(entries))
    bsg.build(["en", "zh"], output, scaffold=False)
    after = path.read_text()

    assert "PART-123" not in after
    assert after.count("data-pid=") == 2
    assert 'data-q="Quote the parts"' in after
    assert "Other shop" in after
    assert (output / bsg.LANG_FILENAME[other_lang]).read_bytes() == other_before
    if change == "replace_supplier":
        assert "Example shop" not in after
        assert "Replacement shop" in after
        assert 'data-q="NEW-456"' in after
        assert after.count('<dialog class="supplier-guide"') == 1
        assert after.count('data-guide="supplier-guide-0-0"') == 1
        assert 'id="supplier-guide-0-0"' in after
    else:
        assert "<dialog" not in after
        assert "data-guide=" not in after


@pytest.mark.parametrize("lang", ["en", "zh"])
@pytest.mark.parametrize(
    "name_fields",
    [{}, {"name": None}, {"name": ""}, {"name": "—"}, {"name": {"en": "", "zh": ""}}],
    ids=["missing", "null", "blank", "dash", "localized-blank"],
)
def test_hidden_supplier_has_no_orphan_dialog(ordering_supplier, lang, name_fields):
    ordering_supplier.pop("name")
    ordering_supplier.update(name_fields)
    rows = [dict(bom_row("p1"), qty="1", desc="")]
    entries = [{"part_id": "p1", "suppliers": [ordering_supplier]}]

    document = bsg.render_document(rows, entries, "", lang)

    assert "<dialog" not in document
    assert "data-guide=" not in document
    assert "PART-123" not in document


@pytest.mark.parametrize("ditto", ["Ditto", ["Ditto", "Ditto"]])
def test_supplier_guide_targets_follow_reordered_slots(ordering_supplier, ditto):
    rows = [dict(bom_row(pid), qty="1", desc="") for pid in ("p1", "p2", "p3")]
    suppliers = [ordering_supplier, {"name": "Other shop"}]
    entries = [
        {"part_id": "p1"},
        {"part_id": "p2", "suppliers": suppliers},
        {"part_id": "p3", "suppliers": ditto},
    ]
    for slot in (0, 1):
        document = bsg.render_document(rows, entries, "", "en")
        targets = re.findall(r'data-guide="([^"]+)"', document)
        dialogs = re.findall(r'<dialog class="supplier-guide" id="([^"]+)"', document)
        assert targets == dialogs == [f"supplier-guide-1-{slot}"]
        suppliers.reverse()


def test_supplier_guide_reuses_inquiry_copy_controls(ordering_supplier):
    rendered = bsg.render_supplier_guides([{"suppliers": [ordering_supplier]}], "en")
    assert bsg._inquiry_button("PART-123", "en", label="Copy") in rendered
    assert "<h2" not in rendered


@pytest.mark.parametrize(
    ("lang", "part_heading", "qty_heading", "copy_label"),
    [("en", "Part / machining", "Qty", "Copy"), ("zh", "零件 / 加工", "数量", "复制")],
)
def test_supplier_guide_escapes_part_data_and_preserves_inline_instructions(
    lang, part_heading, qty_heading, copy_label
):
    details = {
        "heading": "Parts & quantities",
        "instructions": 'Open <a href="https://example.com/">Quote</a>.',
        "parts": [
            {
                "part_number": 'PART-<&"',
                "qty": 2,
                "description": "Length < 50 mm & black",
            }
        ],
    }

    rendered = bsg.render_supplier_guide_body(details, lang)

    assert "<h3>Parts &amp; quantities</h3>" in rendered
    assert '<p>Open <a href="https://example.com/">Quote</a>.</p>' in rendered
    assert "<code>PART-&lt;&amp;&quot;</code>" in rendered
    assert 'data-q="PART-&lt;&amp;&quot;"' in rendered
    assert f'aria-label="{copy_label}: PART-&lt;&amp;&quot;"' in rendered
    assert "Length &lt; 50 mm &amp; black" in rendered
    assert '<span class="guide-qty">2</span>' in rendered
    assert f'<th scope="col">{part_heading}</th>' in rendered
    assert f'<th scope="col">{qty_heading}</th>' in rendered
