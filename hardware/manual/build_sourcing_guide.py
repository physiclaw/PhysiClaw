#!/usr/bin/env python3
"""Render the bilingual PhysiClaw sourcing guide from the manual BOM + supplier data.

The parts list is read straight from the manual's bill-of-materials content
(the ``"bom"``-type page in ``content/``), so the sourcing guide can never
drift from the assembly manual — class, component, spec, qty and application
render verbatim from those rows, each carrying a short stable ``part_id``
(e.g. ``rail-y``).

``sourcing_vendors.json`` is an array with one entry per BOM row, in BOM
order, keyed by ``part_id``. Each entry adds the sourcing columns of the
table — all optional:

- ``ref`` — 参考价: a rough expected cost for the needed quantity, so the
  buyer can judge whether a shop's price is reasonable (missing -> em dash);
- ``inquiry`` — 询价说明: a ready-to-paste message for the shop (part /
  spec / qty, one fact per line), copied via the cell's button; missing ->
  generated from the BOM row, which is right for most parts;
- ``suppliers`` — 供应商 1..3: up to three shops, each ``{name, url?,
  product?}``: the name (linked to ``url``) over the shop's own ``product``
  spec. Missing slots render as pending. Localizable values take
  ``{"en":…,"zh":…}``; plain strings show in both languages.

The special value ``"Ditto"`` makes a field merge with the row above — the
table cell spans both rows, exactly like a ditto mark in a paper ledger. Use
it when consecutive parts are sourced together: the frame extrusions carry
one ref / inquiry / suppliers set on their first row and ``"Ditto"`` on the
rest, so the cutting order reads as one spanned block. ``"Ditto"`` works per
field (``ref`` / ``inquiry`` / ``suppliers``), or per supplier slot inside
the ``suppliers`` array; on the first row it is an error.

Run under ``uv`` from the repo root (standard library only, Python 3.12+);
all paths resolve relative to this file, so the cwd does not matter::

    uv run hardware/manual/build_sourcing_guide.py              # en + zh
    uv run hardware/manual/build_sourcing_guide.py --lang en    # English only -> sourcing_guide.html
    uv run hardware/manual/build_sourcing_guide.py --scaffold   # sync sourcing_vendors.json with the BOM, then build

``--scaffold`` inserts a bare ``{"part_id": …}`` entry for every BOM row
missing from the file, reorders entries to BOM order, and reports stale ids;
it never expands entries with defaults.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

# Shared with the manual build: localization, BOM row grouping, step timing,
# content loading and the html-lang / masthead-mark conventions.
from build_manual import (
    CONTENT_DIR, HTML_LANG, URL_MARK, BuildError,
    _rowspans, _step, load_pages, loc,
)

# --------------------------------------------------------------------------- #
# Paths — everything is resolved relative to this file so cwd does not matter.
# --------------------------------------------------------------------------- #
SCRIPT_DIR = Path(__file__).resolve().parent
VENDOR_FILE = SCRIPT_DIR / "sourcing_vendors.json"
STYLES_CSS = SCRIPT_DIR / "sourcing_styles.css"
# Own output dir — build_manual._clear_output_dir wipes *.html in output/manual,
# so the sourcing guide must not live there.
OUTPUT_DIR = SCRIPT_DIR / ".." / "output" / "sourcing"

LANG_FILENAME = {"en": "sourcing_guide.html", "zh": "physiclaw采购指南.html"}

SUPPLIERS_PER_ROW = 3  # supplier slots per part
DITTO = "Ditto"        # field value: merge this cell with the row above

# UI chrome strings (content strings come localized from the JSON sources).
UI = {
    "doc_title": {"en": "PhysiClaw.ai Sourcing Guide", "zh": "PhysiClaw.ai 采购指南"},
    "h1": {"en": "Sourcing guide", "zh": "采购指南"},
    "lede": {
        "en": "Every part from the assembly manual's bill of materials, "
              "with three suppliers per part.",
        "zh": "对应装配手册物料清单的全部零件，每项零件列出三家供应商。",
    },
    "pending": {"en": "to be found", "zh": "待补充"},
    "copy": {"en": "copy", "zh": "复制"},
    "supplier_n": {"en": "Supplier", "zh": "供应商"},
    "th_cls": {"en": "Class", "zh": "类别"},
    "th_component": {"en": "Component", "zh": "组件"},
    "th_spec": {"en": "Spec", "zh": "规格"},
    "th_qty": {"en": "Qty", "zh": "数量"},
    "th_desc": {"en": "Application", "zh": "用途"},
    "th_ref": {"en": "Ref. price", "zh": "参考价"},
    "th_inquiry": {"en": "Inquiry message", "zh": "询价说明"},
}


def ui(key: str, lang: str) -> str:
    return loc(UI[key], lang)


# --------------------------------------------------------------------------- #
# Data — BOM rows from the manual content, sourcing entries from the data file.
# --------------------------------------------------------------------------- #
def load_bom_rows() -> list[dict]:
    """The ``bom`` page rows from the manual content, in order — located by
    page type (not filename) via the same loader the manual build uses, so a
    renamed or split content file cannot desync the two builds. Every row
    must carry a unique ``part_id`` — that id is the sourcing data's key, so
    a missing or duplicated one would silently orphan entries."""
    rows = [row for page in load_pages() if page.get("type") == "bom"
            for row in page.get("rows", [])]
    if not rows:
        raise BuildError(f"no 'bom' page rows found in {CONTENT_DIR}")
    missing = [f'{r["component"]["en"]} | {r["spec"]["en"]}'
               for r in rows if not r.get("part_id")]
    if missing:
        raise BuildError("BOM row(s) missing a part_id:\n"
                         + "\n".join(f"    {k}" for k in missing))
    ids = [r["part_id"] for r in rows]
    if len(ids) != len(set(ids)):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise BuildError("duplicate part_id(s) in the BOM:\n"
                         + "\n".join(f"    {i}" for i in dupes))
    return rows


def default_inquiry(row: dict) -> dict:
    """A ready-to-paste vendor inquiry built from the BOM row — one fact per
    line (part / spec / qty / quote request), no greeting, so the shop can
    read the requirement at a glance."""
    return {
        "en": (f"{row['component']['en']}\n"
               f"Spec: {row['spec']['en']}\n"
               f"Qty: {row['qty']}\n"
               "Please quote incl. shipping."),
        "zh": (f"{row['component']['zh']}\n"
               f"规格：{row['spec']['zh']}\n"
               f"数量：{row['qty']}\n"
               "请报价（含运费）"),
    }


def load_sourcing_data() -> list[dict]:
    """Read sourcing_vendors.json (an array, one entry per BOM row),
    bootstrapping an empty list on first run."""
    if not VENDOR_FILE.exists():
        return []
    data = json.loads(VENDOR_FILE.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise BuildError(f"{VENDOR_FILE.name} must be a JSON array "
                         "(one entry per BOM row, keyed by part_id)")
    return data


def sync_entries(rows: list[dict],
                 entries: list[dict]) -> tuple[list[dict], list[str], list[str]]:
    """Align the entry array with the BOM: one entry per row, in BOM order.
    Missing rows get a bare ``{"part_id": …}``; entries whose id no longer
    matches a row are dropped and reported. Authored entries pass through
    untouched. Returns ``(synced, added_ids, stale_ids)``."""
    ids = [e.get("part_id") for e in entries]
    dupes = sorted({i for i in ids if i and ids.count(i) > 1})
    if dupes:
        raise BuildError(f"duplicate part_id(s) in {VENDOR_FILE.name}:\n"
                         + "\n".join(f"    {i}" for i in dupes))
    by_id = {e.get("part_id"): e for e in entries}
    row_ids = {r["part_id"] for r in rows}
    synced: list[dict] = []
    added: list[str] = []
    for row in rows:
        pid = row["part_id"]
        if pid not in by_id:
            added.append(pid)
        synced.append(by_id.get(pid) or {"part_id": pid})
    stale = [i for i in ids if i not in row_ids]
    return synced, added, stale


def write_sourcing_file(entries: list[dict]) -> None:
    VENDOR_FILE.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Ditto resolution — per column, "Ditto" merges a cell with the row above.
# --------------------------------------------------------------------------- #
def ditto_spans(values: list, col: str, ids: list[str]) -> list[int]:
    """Rowspan per row for one column: a non-Ditto value opens a cell of span
    1; each following ``"Ditto"`` extends that cell and gets span 0 (no cell
    emitted). ``"Ditto"`` on the first row has nothing to merge with."""
    spans = [0] * len(values)
    anchor = -1
    for i, value in enumerate(values):
        if value == DITTO:
            if anchor < 0:
                raise BuildError(f'"{DITTO}" in {col!r} of entry {ids[i]!r} '
                                 "has no row above to merge with")
            spans[anchor] += 1
        else:
            anchor = i
            spans[i] = 1
    return spans


def supplier_columns(entries: list[dict]) -> list[list]:
    """The three supplier columns as per-row value lists. An entry's
    ``suppliers`` may be ``"Ditto"`` (all three slots merge), an array of up
    to three slots (each a supplier dict or ``"Ditto"``), or missing."""
    columns: list[list] = [[] for _ in range(SUPPLIERS_PER_ROW)]
    for entry in entries:
        suppliers = entry.get("suppliers")
        if suppliers == DITTO:
            slots = [DITTO] * SUPPLIERS_PER_ROW
        elif suppliers is None:
            slots = [None] * SUPPLIERS_PER_ROW
        else:
            slots = list(suppliers)[:SUPPLIERS_PER_ROW]
            slots += [None] * (SUPPLIERS_PER_ROW - len(slots))
        for j in range(SUPPLIERS_PER_ROW):
            columns[j].append(slots[j])
    return columns


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _span_attr(span: int) -> str:
    return f' rowspan="{span}"' if span > 1 else ""


def render_supplier_cell(supplier: dict | None, lang: str, span: int) -> str:
    """One supplier cell: the shop name (linked to its url) over the shop's
    own product spec."""
    name = loc(supplier.get("name") or "", lang) if supplier else ""
    if not name:
        return f'<td class="offer pending"{_span_attr(span)}>{ui("pending", lang)}</td>'
    name = html.escape(name)
    if supplier.get("url"):
        href = html.escape(supplier["url"], quote=True)
        name = f'<a href="{href}" target="_blank" rel="noopener">{name}</a>'
    product = loc(supplier.get("product") or "", lang)
    product_html = f'<div class="v-prod">{html.escape(product)}</div>' if product else ""
    return (f'<td class="offer"{_span_attr(span)}><div class="v-vendor">{name}</div>'
            f"{product_html}</td>")


def _inquiry_cell(message: str, lang: str, span: int) -> str:
    # &#10; keeps the message's line breaks intact inside the data attribute,
    # so the copied text pastes as the same multi-line message shown.
    msg_attr = html.escape(message, quote=True).replace("\n", "&#10;")
    return (f'<td class="inq"{_span_attr(span)}><span class="msg">{html.escape(message)}</span>'
            f'<button class="copy" type="button" data-q="{msg_attr}">'
            f'{ui("copy", lang)}</button></td>')


def render_table(rows: list[dict], entries: list[dict], lang: str) -> str:
    """The grouped parts table: BOM columns rendered verbatim from the manual
    content, then the sourcing columns (ref price / inquiry / one column per
    supplier slot). ``"Ditto"`` fields merge their cell with the row above."""
    ids = [r["part_id"] for r in rows]
    cls_span = _rowspans(rows, lambda r: r["cls"]["en"])
    comp_span = _rowspans(rows, lambda r: (r["cls"]["en"], r["component"]["en"]))

    refs = [e.get("ref") for e in entries]
    inquiries = [e.get("inquiry") for e in entries]
    sup_cols = supplier_columns(entries)
    ref_spans = ditto_spans(refs, "ref", ids)
    inq_spans = ditto_spans(inquiries, "inquiry", ids)
    sup_spans = [ditto_spans(col, f"suppliers[{j}]", ids)
                 for j, col in enumerate(sup_cols)]

    body: list[str] = []
    for idx, row in enumerate(rows):
        cells = ""
        if cls_span[idx]:
            cells += f'<td class="cls"{_span_attr(cls_span[idx])}>{loc(row["cls"], lang)}</td>'
        if comp_span[idx]:
            cells += (f'<td class="comp"{_span_attr(comp_span[idx])}>'
                      f'{loc(row["component"], lang)}</td>')
        cells += (f'<td class="spec">{loc(row["spec"], lang)}</td>'
                  f'<td class="qty">{row["qty"]}</td>'
                  f'<td class="desc">{loc(row["desc"], lang)}</td>')

        if ref_spans[idx]:
            ref = loc(refs[idx] or "", lang) or "—"
            cells += f'<td class="ref"{_span_attr(ref_spans[idx])}>{html.escape(ref)}</td>'
        if inq_spans[idx]:
            message = loc(inquiries[idx] or "", lang) or loc(default_inquiry(row), lang)
            cells += _inquiry_cell(message, lang, inq_spans[idx])
        for j in range(SUPPLIERS_PER_ROW):
            if sup_spans[j][idx]:
                cells += render_supplier_cell(sup_cols[j][idx], lang, sup_spans[j][idx])

        # Only class starts get a heavier separator; the full cell grid
        # already delineates components, so no `sub` class here.
        row_cls = ' class="grp"' if cls_span[idx] else ""
        body.append(f"<tr{row_cls}>{cells}</tr>")

    supplier_ths = "".join(
        f'<th>{ui("supplier_n", lang)} {j + 1}</th>' for j in range(SUPPLIERS_PER_ROW))
    head = (f'<tr><th>{ui("th_cls", lang)}</th><th>{ui("th_component", lang)}</th>'
            f'<th>{ui("th_spec", lang)}</th><th>{ui("th_qty", lang)}</th>'
            f'<th>{ui("th_desc", lang)}</th><th>{ui("th_ref", lang)}</th>'
            f'<th>{ui("th_inquiry", lang)}</th>{supplier_ths}</tr>')
    return (f'<div class="table-wrap"><table class="sourcing">'
            f"<thead>{head}</thead><tbody>{''.join(body)}</tbody></table></div>")


# Page script: copy-to-clipboard for the per-row inquiry message. Both file://
# and https:// are secure contexts, so navigator.clipboard is always there.
PAGE_JS = """\
document.querySelectorAll('button.copy').forEach(function (b) {
  b.addEventListener('click', function () {
    navigator.clipboard.writeText(b.dataset.q).then(function () {
      b.classList.add('ok');
      setTimeout(function () { b.classList.remove('ok'); }, 1200);
    });
  });
});
"""


def render_document(rows: list[dict], entries: list[dict], css: str, lang: str) -> str:
    body = (
        '<div class="wrap">'
        f'<header class="mast"><h1>{ui("h1", lang)}</h1>'
        f'<span class="url">{URL_MARK}</span></header>'
        f'<p class="lede">{ui("lede", lang)}</p>'
        f"{render_table(rows, entries, lang)}</div>"
    )
    return (
        f'<!DOCTYPE html>\n<html lang="{HTML_LANG[lang]}">\n<head>\n'
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{ui('doc_title', lang)}</title>\n"
        f"<style>\n{css}</style>\n</head>\n<body>\n{body}\n"
        f"<script>\n{PAGE_JS}</script>\n</body>\n</html>\n"
    )


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #
def build(langs: list[str], out_dir: Path, scaffold: bool) -> list[Path]:
    with _step("load bom + sourcing"):
        rows = load_bom_rows()
        entries, added, stale = sync_entries(rows, load_sourcing_data())
    if scaffold or not VENDOR_FILE.exists():
        write_sourcing_file(entries)
        print(f"  synced {VENDOR_FILE.name}: +{len(added)} new empty entr(y/ies)")
    elif added:
        print(f"  note: {len(added)} BOM row(s) have no sourcing entry "
              f"(rendered with defaults) — run with --scaffold to add them")
    if stale:
        print(f"  warning: {len(stale)} stale entr(y/ies) no longer match a BOM part_id:")
        for i in stale:
            print(f"    {i}")

    css = STYLES_CSS.read_text(encoding="utf-8")
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for lang in langs:
        path = out_dir / LANG_FILENAME[lang]
        with _step(f"render html [{lang}]"):
            path.write_text(render_document(rows, entries, css, lang), encoding="utf-8")
        written.append(path)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--lang", choices=("en", "zh", "all"), default="all",
        help="language(s) to render (default: all)",
    )
    parser.add_argument(
        "--out", type=Path, default=OUTPUT_DIR,
        help="output directory (default: ../output/sourcing)",
    )
    parser.add_argument(
        "--scaffold", action="store_true",
        help="sync sourcing_vendors.json with the BOM (add bare part_id entries) before building",
    )
    args = parser.parse_args()

    langs = ["en", "zh"] if args.lang == "all" else [args.lang]
    out = args.out.resolve()
    shown = out.relative_to(Path.cwd()) if out.is_relative_to(Path.cwd()) else out
    print(f"building sourcing guide [{', '.join(langs)}] -> {shown}")
    try:
        written = build(langs, out, scaffold=args.scaffold)
    except BuildError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    print(f"\ndone — wrote {len(written)} file(s):")
    for path in written:
        print(f"  {path.name}")


if __name__ == "__main__":
    main()
