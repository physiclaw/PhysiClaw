# PhysiClaw — Hardware

**The `hardware` module delivers two documents — all you need to make your own
PhysiClaw hardware:** the **Assembly Manual** shows how to put the machine
together, step by step, and the **Sourcing Guide** lists every part — its spec,
where to buy it, and roughly what it costs. Everything else here — the parts,
the assembly steps, the renderer, the build cache — exists only to generate
those two.

It's **CAD-as-code**: every part, every assembly step, and both documents are
generated from Python — there is no GUI CAD file to hand-edit, and re-running
the scripts reproduces every artifact from source.

The geometry kernel is [build123d](https://build123d.readthedocs.io)
(OpenCASCADE under the hood); the manual and sourcing builders are plain
standard-library Python.

---

## Pipeline at a glance

```text
parts/      standard + custom parts        →  STEP solids
   │
   ▼
assembly/   compose parts into ~70 steps   →  STEP + SVG line-art  (+ per-step Markdown BOM)
   │        each step's placement derives from the upstream chain
   ▼
mark/       annotate the step SVGs         →  snapshot SVGs (raw if unmarked)
   │
   ▼
documents/manual/    chapter JSON + step SVGs → bilingual HTML / PDF

documents/bom/bom.json   authored purchasing list
   ├─ documents/manual/   → Section 11 of the assembly manual
   ├─ documents/sourcing/ + market-specific vendors → bilingual HTML
   └─ documents/drawing/  + model dimensions → cut-and-drill sheets (HTML / PDF)
```

All generated files land under `output/` and are not committed.

---

## Directory layout

```text
hardware/
├── __main__.py            Unified CLI front door (python -m hardware <subcommand>)
├── scheme.py              Shared naming scheme: output dirs, stem convention,
│                          variant/filename builders + their matching regexes
├── check.py               Static cross-artifact consistency check (CI gate)
│
├── parts/                 Parametric part definitions
│   ├── base.py            BasePart: build/export, geometry cache, BOM registry
│   ├── _fits.py           Shared tolerances (clearance holes, nut dims, pitches)
│   ├── standard/          Off-the-shelf parts (screws, nuts, rail, motor, …)
│   ├── custom/            3D-printed / machined parts (clamps, joints, mounts)
│   ├── export_standard.py Export all standard parts → output/step/
│   ├── export_custom.py   Export all custom parts → output/step/
│   └── build_custom_parts.py  Bundle custom STEPs + manifests → print_3d/*.zip
│
├── assembly/              Assembly steps and rendering
│   ├── base.py            BaseAssembly: STEP export + two-variant SVG render
│   ├── projection.py      Camera model + FreeCAD-view → Camera() helper
│   ├── dispatch.py        Procedure discovery, family ordering, batching, retry
│   ├── build_procedures.py  Build & render every step (the main driver)
│   ├── bom.py             BOM library (collect / delta / write_bom)
│   ├── procedures/        ~70 assembly-step modules (<family>_<NN>_<name>.py)
│   ├── patch/             Saved annotation ops, one JSON per drawing
│   └── mark/              Browser tool to annotate step SVGs
│
├── documents/             Bilingual builder documents (standard-library only)
│   ├── MANUAL_VERSION     Shared document date, updated by the user
│   ├── common.py / pdf.py Localization, typography, tables, timing and PDF rendering
│   ├── bom/
│   │   ├── bom.json       Authored purchasing rows, keyed by part_id
│   │   └── loader.py      Shared purchasing-list loading and validation
│   ├── manual/
│   │   ├── build.py       Chapter JSON + SVGs → HTML / PDF
│   │   ├── content/       13 ordered chapters; Section 11 references the shared BOM
│   │   ├── styles.css
│   │   └── assets.py / icon_svg.py / pages.py / paginate.py / style_checks.py
│   ├── sourcing/
│   │   ├── build.py       Shared purchasing list + vendor data → HTML
│   │   ├── styles.css
│   │   ├── optional_tools.json
│   │   └── vendors/       cn.json and global.json, keyed by part_id
│   └── drawing/
│       └── extrusion.py  Model dimensions + purchasing list → A4 cut-and-drill sheets
│
└── output/                Generated artifacts (git-ignored)
    ├── step/  svg/  bom/  manual/  sourcing/  drawing/  print_3d/  render/
```

---

## Design

**Parts register themselves.** Every `BasePart.build()` does two things beyond
returning geometry: it pushes one row (`bom_key`, `qty`, `category`) into a
process-wide BOM registry, and — for leaf parts — it caches the built solid by
`geom_key` so repeated instances are a cheap copy instead of a rebuild. Parts
are tagged `standard` (purchasable) or `custom` (manufactured for this build).

**Assemblies derive their placement.** Each step is a `BaseAssembly` that
embeds its predecessor and positions new parts **relative to the upstream
chain** rather than from hardcoded coordinates — a clamp's position is computed
by walking the rail → carriage → joint math. This keeps the model
self-consistent: change a part dimension and every downstream step follows.
Assemblies deliberately opt **out** of the geometry cache (caching a whole
compound deep-copies the entire tree); they are recomposed from cached leaves
instead.

**Two variants per step.** Every step builds both `_exploded` (install motion,
with ghost layers) and `_assembled` (finished state) and exports a STEP for
each; SVGs are named `<step>_<variant>_cam<i>.svg`. A class-level `views`
declaration marks which (variant, camera) drawings the manual actually uses —
only those render (each skipped view saves an exact-HLR pass, the dominant
cost and the crash surface). No `views` means render every camera for both
variants. Camera indices stay stable, so trimming never renames files.

**Built in subprocesses.** `build_procedures` groups steps by family in
dependency order and runs each batch in its own subprocess to bound OCCT
memory use. Crashed steps retry independently with `--only-missing`, retaining
finished outputs. Concurrency and crash recovery are described below.

**Naming convention.** Procedure and part files follow
`<family>_<NN>_<descriptor>.py`, where `NN` orders steps within a family in
gaps of 10. Families build in dependency order: `fastener → frame → idler →
motor → linear → belt → tapz → phone → board → camera → wire`.

---

## Prerequisites

- **[uv](https://docs.astral.sh/uv/)** — runs everything; resolves
  dependencies on demand.
- **`--group cad`** — pulls in build123d. Required for any script that touches
  geometry (parts, assemblies, BOM). The manual and sourcing builders are
  standard-library only and need no group.
- **A Chromium-family browser** is required when `--pdf` is requested and for release preparation. HTML-only builds do not need it.

Run all commands **from the repo root**.

---

## Script usage

One entry point drives every stage. List the subcommands and their flags
with `--help`:

```bash
uv run --group cad python -m hardware --help
```

The subcommands — each forwarding its flags to the stage it wraps:

| subcommand | stage |
|---|---|
| `parts` | export part STEPs → `output/step/` |
| `build` | build assembly steps (STEP + SVG; `--bom` adds the BOM) |
| `check` | static consistency check — stems, patches, manual figures, sourcing ids |
| `step <stem>` | build one step via `build --bom --stems` (both variants) |
| `print` | 3D-print package → `output/print_3d/*.zip` |
| `manual` | bilingual HTML / PDF manual → `output/manual/` |
| `sourcing` | sourcing guide → `output/sourcing/` |
| `drawing` | A4 extrusion cut-and-drill drawings (`--pdf`) → `output/drawing/` |
| `release` | prepare, verify, preflight, publish or deploy a verified release bundle |
| `mark` / `replay` | annotate step SVGs / replay saved patches |
| `refit <json> --parts …` | recompute a patch's highlights as the exact silhouettes of the named parts (by label prefix), then replay |
| `camera` | FreeCAD camera view → `Camera()` literal |

Geometry subcommands need `--group cad`; `check`, `manual`, `sourcing` and `drawing`
are standard-library only. Each stage module is also runnable on its own (e.g.
`uv run --group cad python -m hardware.parts.custom.solenoid_mount`).

`build` is incremental: each step's outputs are cached under
`output/.cache/` keyed by a content hash of its import closure (source +
parts + predecessors + package initializers), the toolchain lock and platform,
and its patch JSON. Annotated drawings are also bound to the exact geometry-cache
manifest, so an interrupted update cannot mix old drawings with new geometry.
Restored files are hash-checked in temporary files before atomic replacement,
so a cancelled or corrupt copy does not replace a complete output. The resolved
cache directory is saved in the log. Unchanged
steps are restored instead of rebuilt+re-rendered — and a patch-only edit
just re-runs the cheap annotation replay. Pass `--no-cache` to force a full
rebuild — the rebuilt outputs still refresh the cache. (`step` always
rebuilds its one stem.)

The dispatcher records source and annotation keys before work and rechecks them
before success. Cache stores verify those keys and copied bytes before publishing
a manifest, including when saving completed work after cancellation. Input edits
during a build fail the run. Only requested BOM kinds are cached, so an old BOM
left on disk cannot satisfy a later `--bom` or `--bom-delta` request.

Full builds run two independent CAD batch processes by default. Use
`build --jobs 1` on a low-memory machine, or up to `--jobs 4` when more
memory and CPU cores are available. Each heavy worker can use about 1.2 GB.
Workers never share an OCCT interpreter, and the output lock still rejects
concurrent build commands targeting the same directory.

### Terminal output and logs

Hardware generators, checks, releases and annotation tools use the same terminal
renderer: a title, completed tasks with elapsed time in the last column, and one
`SUCCESS`, `FAILED` or `CANCELLED` result. Times are measured from command start;
individual task durations are saved in JSON. CAD builds count completed assemblies,
including cache restores, so parallel workers can finish out of source order.
Repeated verification does not repeat successful rows.

```text
Hardware build / 71 assemblies

   Step Assembly                     Elapsed
--------------------------------------------
68 cached; 3 to build                   0.3s
[69/71] fastener_22_tnut_standard       7.0s
[70/71] fastener_10_BHCS                7.1s
[71/71] fastener_11_SHCS                7.1s
--------------------------------------------
SUCCESS  71/71 complete                 7.3s
68 cached, 3 built
Output: hardware/output
```

Routine output omits `INFO`. Warnings and periodic worker status stay hidden even
with `--verbose`, which otherwise adds diagnostics and severity levels. Errors
retain `ERROR:`. Extra details wrap below task names; long
names and paths wrap without truncation. Narrow panes shrink the name column
while keeping timings aligned. Pipes and CI use 120 columns when neither a
terminal size nor `COLUMNS` is available. Interactive terminals use a bold title,
muted progress and timings, green success and red errors. `NO_COLOR` (even empty)
or `TERM=dumb` disables color; pipes and JSON stay plain. Output uses no cursor
movement and reads cleanly through `tee`.

Each command saves a separate UTF-8 `output/logs/build-<run>.jsonl`, except help,
camera conversion and failures before a run starts. `PHYSICLAW_HW_OUTPUT` selects
the log root; a document's `--out` only changes its document directory. All debug
events, warnings, retries, timings, worker stdout/stderr and Python tracebacks are
saved and flushed as they arrive. Python warnings include category and source
location. Workers use unbuffered output and record both supervisor and actual
process return codes. Logs persist until removed; their path appears in verbose
output, JSON and failure summaries. Saved records contain no added styling.

Use the same presentation flags across commands; Make suppresses recipe echo:

```bash
make hw-build ARGS="--bom --verbose"          # variant timings and diagnostics
make hw-build ARGS="--bom --log-format json" # every event as JSONL on stdout
make hw-step ARGS="fastener_10_BHCS --log-format json"
make hw-check ARGS="--log-format json"
make hw-manual ARGS="--verbose"
make hw-verify HW_VERSION=X.Y ARGS="--log-format json"
```

JSON records contain `schema_version: 1`, `command`, `run_id`, collector `sequence`,
UTC `timestamp`, `elapsed_s`, `level`, `event`, `message` and producer `pid`.
Agents should use event names and typed fields instead of parsing message wording.
JSON includes debug events without `--verbose`, with one record per physical line.
Worker output is wrapped in `worker_output` events. Worker records add `worker_pid`;
assembly records identify `stem` and optionally `variant`. Timings such as
`build_s`, `export_s`, `render_s` and `duration_s` are seconds. Sequence describes
receipt order; producer timestamps describe when events originated.

Non-CAD commands use `command_started`, `task_started`, `task_finished` and
`command_finished`. Task events identify `item`; successful completions include
`completed` and `duration_s`. Final events include `status` and `exit_code`.
Release commands show stage completions and save child JSON under
`subprocess_event.child_event`. Before removing private release output, they copy
stage JSONL and native crash logs beside the parent log into
`build-<run>.children/`; `diagnostic_saved` links original and retained paths.

Native CAD stacks are saved in `build-<run>.crash.log`, shared by the run's workers
and retries and linked from JSON via `crash_log`. Startup faults before this file
opens are captured from stderr. Later builds never clear earlier logs. Recovery
retains completed outputs; only `build_finished` with `status: "success"` and exit
code zero confirms success. Failed verification still fails even if files exist.

Ctrl+C stops queued batches and retries, interrupts active workers and escalates
to termination if native code does not respond. The CLI waits for cleanup before
releasing its output lock, keeps completed files and logs, saves interruption
details, and shows `CANCELLED` without a Python traceback. It returns 130; Make may
print `Error 130` and return its own nonzero status. Argument and lock errors use
stderr before a run starts. Help retains standard usage formatting, and
`hw-camera` prints a copyable `Camera(...)` literal without a table.

After workers stop, cancellation caches assemblies whose latest attempt reported
success and whose required outputs are complete, including those in unfinished
recovery batches. With unchanged sources and valid cache entries, the next build
restores those assemblies. A progress label such as `[45/71]` counts completions
in the current run, including restored assemblies; it is not a source-list index.
Failed, unfinished and unreported attempts are rebuilt. Cache-write problems are
saved in the run log; changed source files or damaged cache entries also rebuild.

Exact hidden-line rendering can crash inside OpenCascade's rejection cache
([upstream issue #1546](https://github.com/Open-Cascade-SAS/OCCT/issues/1546)).
The fault is present in the pinned kernel and released 8.0.1; an upstream
[cache replacement](https://github.com/Open-Cascade-SAS/OCCT/pull/1489) has
landed on its development branch. Until a compatible fixed release is
available, crashed workers restart with a bounded retry budget. Completed
STEPs, BOMs and drawings are retained, and temporary exact BREP geometry in
`output/.render-work/` lets unfinished views resume without rebuilding their
assembly. Checkpoints retain both drawing layers and the original cameras,
are checked for corruption, and are removed after success or before a fresh
build. Exhausted retries and incomplete outputs still fail the build.

Run `make hw-help` for the hardware commands, grouped by task. The main workflow is:

| Command | Result |
| --- | --- |
| `make hw-build ARGS=--bom` | Build local CAD outputs with the cache. |
| `make hw-prepare HW_VERSION=X.Y` | Prepare and verify a local release bundle with the cache. No upload. |
| `make hw-verify HW_VERSION=X.Y` | Check an existing bundle. No rebuild or upload. |
| `make hw-publish HW_VERSION=X.Y` | Publish a prepared bundle, then trigger the docs hook. |
| `make hw-release HW_VERSION=X.Y` | Prepare, verify, publish and trigger the docs hook in one command. |

Commit and push main before preparing a bundle intended for publication.
Use `make docs-deploy` to retry only the docs hook. `hw-rebuild` explicitly bypasses
the CAD cache; routine builds and releases use the cache.

For individual outputs, pass flags through `ARGS`:

```bash
make hw-parts                     # export part STEPs
make hw-build ARGS="--bom"        # build steps + cumulative BOM
make hw-check                     # static consistency check (also run in CI)
make hw-step ARGS=belt_20_clamp   # build one step (= build --bom --stems)
make hw-print                     # 3D-print package (zip)
make hw-manual                    # build the assembly manual (HTML)
make hw-manual-pdf                # build the assembly manual, also as PDF
make hw-sourcing                  # build sourcing guide
make hw-drawing ARGS=--pdf        # build the extrusion tech drawing (HTML, and PDF)
make hw-mark ARGS=<svg|json>      # annotate a step drawing
pbpaste | make hw-camera          # FreeCAD view → Camera() literal
make hw-rebuild                   # all local stages, bypass CAD cache
make hw-help                      # show commands by task
```

The sourcing builder reads `documents/sourcing/vendors/cn.json` for `--lang zh` and
`documents/sourcing/vendors/global.json` for `--lang en`; the default builds both. Each
file uses the same entry schema and shared BOM `part_id` values, but suppliers
can be maintained independently. Vendor text is stored as plain Chinese strings
in the CN file and plain English strings in the global file. `--scaffold` syncs the selected language files
(or both by default) with the BOM.

Each supplier can include optional `details` for an ordering popup: `label`,
`title` (accessible name), `heading`, `instructions` (inline HTML), and `parts`
(`part_number`, `qty`, `description`). Remove `details` to drop the popup, or
remove the entire supplier object to drop both the supplier and its popup.
Rebuild with `make hw-sourcing`; no renderer changes are needed. Whole-row
`"Ditto"` follows the updated supplier list. Per-slot `"Ditto"` follows column
positions, so check those entries when deleting or reordering suppliers.

> **Photoreal render — WIP.** A separate Blender render of the full machine
> (`camera_40_frame`) is being reworked; its scripts were cleared and are not
> currently in the tree. The line-art SVG pipeline is unaffected.

---

## Typical full rebuild

```bash
uv run --group cad python -m hardware parts --custom --standard  # part STEPs
uv run --group cad python -m hardware build --bom                # steps + BOM
uv run --group cad python -m hardware print                      # 3D-print package
uv run            python -m hardware manual --pdf                # the manual + required PDFs
uv run            python -m hardware sourcing                    # the sourcing guide
uv run            python -m hardware drawing --pdf                # the extrusion tech drawing
```

---

## Cutting a release

Each `physiclaw-hardware-vX.Y` release bundles four zips and two PDFs
packaged in a private output tree using validated CAD cache entries:

- **Assembly manual** — the whole `manual/` folder (HTML + PDF in English
  and 中文, plus the step figures).
- **Sourcing guide** — the `sourcing/` folder (HTML in English and 中文)
  together with the `drawing/` folder (the extrusion tech drawing, HTML + PDF).
- **Extrusion tech drawing PDFs** — `physiclaw_extrusion_drawing_en.pdf`
  (EN then ZH, 2 pages) and `_zh.pdf` (ZH, 1 page). The sourcing guide links
  to `releases/latest/download/`; the docs site uses `/downloads/`.
- **Camera frame** — just the assembled camera-frame STEP file, on its own.
- **Custom parts** — the print package from `output/print_3d/`.

The version stamp on the manual's cover comes from `documents/MANUAL_VERSION` —
bump that file when the manual content changes.

Prepare and review a local bundle first (no GitHub writes):

```bash
make hw-prepare HW_VERSION=X.Y
make hw-verify HW_VERSION=X.Y
```

Preparation runs the hardware regression tests and static consistency gate, restores
or builds **all procedures** with BOMs, exports the custom parts, and renders both
language editions of the manual, sourcing guide and extrusion drawings. It uses a
private output directory and shares only the persistent CAD cache. Source keys,
required files and SHA-256 checksums must match before any cached output is restored;
changed or damaged entries rebuild automatically. Manual-only edits reuse CAD work.
Interactive output files are never packaged. Dependencies use `uv --locked`.
Missing Chrome, failed PDFs, annotation errors and incomplete CAD outputs stop the
command. A small PDF render checks browser launch permissions before CAD work starts.

The resulting bundle is stored at
`hardware/output/release/physiclaw-hardware-vX.Y/`: four zips, two PDFs and
`manifest.json`. The manifest records the exact Git commit, a digest of source
files, the Python/CAD toolchain versions and each asset's SHA-256. Verification
checks the exact asset set, checksums, basic file integrity, bilingual documents,
local image references and matching copies of the extrusion PDFs. Repeating preparation
reuses an existing bundle only after it passes verification against the current commit
and sources. Stale or damaged bundles stop the command. To rebuild the same version,
move its generated directory aside after reviewing why it failed.

When publication is intended, commit and push main **before preparation**, then:

```bash
make hw-publish HW_VERSION=X.Y
# Or preflight, prepare, publish and refresh docs in one command:
make hw-release HW_VERSION=X.Y
```

Publishing repeats verification and requires a clean main checkout whose HEAD
matches **live** `origin/main`. Existing local/remote tags, published releases and
drafts targeting another commit stop the command. Network or authentication failures
also stop it. The release targets the recorded commit SHA, never a moving branch.
Assets are uploaded to a draft and checked against GitHub's SHA-256 digests and sizes.
Only assets without a server digest are downloaded for local verification. After a
final source and draft check, that exact draft ID is published and marked latest.
The command then reads GitHub's published latest release back and requires the same
release ID, version, asset IDs, sizes and checksums. It also resolves the actual Git
tag to the bundle's commit; release metadata alone is insufficient. These checks run
before and after the docs hook. Any mismatch or unreadable result returns failure.

If a transfer fails, inspect the error and retry the same command. A matching draft
resumes by uploading only missing assets; valid uploaded files are kept. Unexpected,
incomplete or mismatched assets stop publication. The workflow never overwrites or
deletes assets, drafts or published releases. For a stale draft, inspect and remove
that unpublished draft explicitly before preparing its replacement.

All Make release targets use the same guarded Python entry point, including
the legacy `_hw-package` alias. `make -j` cannot run validation and publishing
out of order. A per-output OS lock rejects concurrent CLI writers and releases
automatically if a process exits. `PHYSICLAW_HW_OUTPUT` can select a private
directory for development or CI builds; release bundles always use the fixed
versioned location above.

Run the hardware regression suite after pipeline changes:

```bash
make hw-check
uv run --locked --group cad pytest tests/hardware
```

CI installs the CAD group on Linux and macOS, runs these tests as part of the
suite, and builds a real fastener STEP/SVG/BOM as a smoke test. Release preparation
uses the same validated CAD cache as normal builds; documents and packaging are fresh.
The gates catch build and packaging mistakes; they do not prove physical fit
or replace engineering review of changed geometry. Repository administrators
must keep CI required and review changes to release guards to prevent bypasses.

After successful publication, both `make hw-publish` and `make hw-release`
automatically run `make docs-deploy` to trigger the docs site's Cloudflare Pages
deploy hook. Its URL lives in the gitignored `.docs-deploy-hook`
at the repo root. The site serves the assembly manual,
the sourcing guide and the extrusion tech drawing from the release assets,
so the live pages update when that deployment finishes. If the hook fails, the
hardware release stays published; retry only `make docs-deploy`. The hook must return
HTTP 2xx within its timeout; redirects and server errors fail the command.

A successful `make hw-release` means the requested release is published as latest,
its tag and assets match the verified bundle, and the docs hook accepted the request.
The Cloudflare site build runs asynchronously; hook success does not mean the new
site has finished deploying. Later external edits to GitHub are outside this check.
