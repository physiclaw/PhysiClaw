# Hardware document sources

Sources for the assembly manual, sourcing guide and extrusion drawings. Website
pages live in [docs/](../../docs/); generated hardware documents are published
as release assets.

## Edit sources

Paths below are relative to this directory. Edit sources, not generated files.

| What to edit | Source |
| --- | --- |
| Required parts, quantities and specifications | `bom/bom.json` |
| Suppliers, prices and ordering instructions | `sourcing/vendors/cn.json` or `sourcing/vendors/global.json` |
| Optional tools and suppliers | `sourcing/optional_tools.json` |
| Assembly instructions and chapter layout | `manual/content/*.json` |
| Manual appearance | `assets/manual/styles.css` |
| Sourcing appearance and interactions | `assets/sourcing/styles.css`, `assets/sourcing/checklist.js` |
| Manual layouts and validation | `manual/build.py`, `manual/schema.py` |
| Extrusion parts and drawings | `drawing/extrusion_parts.py`, `drawing/extrusion.py` |
| Travel dimensions and extrusion profiles | `../assembly/travel_ranges.py`, `../parts/standard/extrusion_spec.py` |
| Document date | `MANUAL_VERSION` (change only when requested; separate from the release version) |

All three documents share `bom/bom.json`. Vendor entries join its stable
`part_id` values; `cn.json` supplies the Chinese edition and `global.json` the
English edition. Keep purchasing rows in the shared BOM, not in chapters or
supplier files, and do not replace them with CAD-generated per-step parts lists.
`manual/content/11_bom.json` controls the manual's BOM presentation through
`"rows_source": "bom"`.

## Build and validate

Run from the repository root. See the [hardware guide](../README.md) for setup,
individual build targets and test commands.

| Command | Result |
| --- | --- |
| `make manual` | CAD models → manual HTML/PDFs → sourcing HTML; stops on failure |
| `make hw-drawing` | Extrusion drawings in HTML and PDF |
| `make hw-sourcing ARGS=--scaffold` | Sync vendor files with the BOM (add missing IDs, remove stale IDs), then build HTML |
| `make hw-check` | Check source consistency without building outputs |

Documents include English and Chinese editions. Outputs default to
`hardware/output/{manual,sourcing,drawing}`; `PHYSICLAW_HW_OUTPUT` changes the
output root. Standalone manual builds require SVGs from `make hw-build`.
PDFs use Chrome/Chromium by default. On Windows, the manual also supports
`make hw-manual-pdf ARGS="--browser edge"`.

Manual builds and `make hw-check` validate content against `manual/schema.py`:

- Use valid JSON without duplicate keys, unique page IDs, valid TOC references
  and one consolidated BOM table. Unknown fields and incorrect types fail.
- Localized text requires `en`; missing, empty or null `zh` falls back to English.
  Figure sources must be SVG filenames without directories.
- Use units for non-zero inline CSS lengths, such as `width` and `margin`.
- When adding layouts or fields, update both `manual/schema.py` and `manual/build.py`.

Review generated pages for visual layout issues after validation passes.

## Supplier URL review

`make hw-url` shows up to three Taobao/Tmall and three AliExpress links from
suppliers, notes and optional tools. It favors links shown least often in the
last 30 days. Every displayed link is recorded as checked, including piped output.
In a terminal, any key opens the next link; `q` or Esc quits. Piping opens no tabs.

History: `hardware/output/.url-checks.json` (follows `PHYSICLAW_HW_OUTPUT`).
Use `ARGS=--verbose` for full specs or `ARGS="--state /path/to/history.json"`
to choose a history file.
