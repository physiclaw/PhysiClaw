# PhysiClaw hardware

Everything you need to build the PhysiClaw rig: parts, assembly instructions,
3D-print packages and frame drawings. The models and documents are generated
from Python source.

## Build your own

Start with the guides and downloads below. Generating files locally is optional.

- **Assembly manual** — step-by-step instructions: [English](https://docs.physiclaw.ai/en/hardware/manual/) · [中文](https://docs.physiclaw.ai/zh/hardware/manual/)
- **Sourcing guide** — parts, suppliers and estimated costs: [English](https://docs.physiclaw.ai/en/hardware/sourcing-guide/) · [中文](https://docs.physiclaw.ai/zh/hardware/sourcing-guide/)
- **[Downloads](https://github.com/physiclaw/PhysiClaw/releases/latest)** — manuals, CAD models, custom parts and frame drawings.

## Generate files locally

Works on macOS, Windows and Linux. Install [uv](https://docs.astral.sh/uv/).
PDF builds and release preparation also need Chrome or Chromium. The `make`
commands need GNU Make; Python alternatives are shown below.
Run all commands from the repository root.

Build the CAD models, assembly manual and sourcing guide:

```sh
make manual
```

This runs `hw-build` → `hw-manual-pdf` → `hw-sourcing`, stopping on failure.
Documents include English and Chinese editions. Builds install locked CAD
dependencies and reuse cached work. Outputs default to `hardware/output/`.

Individual targets (manual builds require illustrations from `make hw-build`):

| Task | Command |
| --- | --- |
| Build assembly models and drawings | `make hw-build` |
| Build the assembly manual (HTML + PDF) | `make hw-manual-pdf` |
| Build the sourcing guide (HTML) | `make hw-sourcing` |
| Build frame cut-and-drill sheets (HTML + PDF) | `make hw-drawing` |
| Package custom parts for 3D printing | `make hw-print` |
| Build one assembly step | `make hw-step ARGS=belt_20_clamp` |
| Include per-step parts lists | `make hw-build ARGS=--bom` |
| Use less memory | `make hw-build ARGS="--jobs 1"` |
| See all commands | `make hw-help` |

Without Make, run these commands in order, stopping if a command fails:

```sh
uv run --locked --group cad python -m hardware build
uv run --locked python -m hardware manual --pdf
uv run --locked python -m hardware sourcing
```

For Python CLI options, run `uv run --locked python -m hardware --help`.

## Directory layout

```text
hardware/
├── parts/                 Part models
│   ├── standard/          Off-the-shelf parts
│   └── custom/            Custom parts
├── assembly/
│   ├── procedures/        Assembly steps
│   └── annotation/        Generated manual highlights and callouts
├── documents/
│   ├── bom/bom.json       Shared purchasing list
│   ├── manual/            Assembly instructions and manual builder
│   ├── sourcing/          Suppliers and sourcing guide builder
│   ├── drawing/           Frame cut-and-drill drawings
│   └── assets/            Styles, scripts, icons and illustrations
├── port/                  Platform-specific helpers
├── __main__.py            Python command-line entry point
├── check.py               Source consistency checks
├── release.py             Release preparation and publishing
└── output/                Generated files (git-ignored)
```

## Make changes

Edit source files, then regenerate the outputs in `hardware/output/`. See the
[document source guide](documents/README.md) for specific files and commands.
After hardware pipeline changes, run:

```sh
make hw-check
uv run --locked --group cad pytest tests/hardware tests/test_architecture.py
```

## Prepare a release

For a release intended for publication, commit and push `main` before preparing.
Replace `X.Y` with the hardware version:

```sh
make hw-prepare HW_VERSION=X.Y   # Build and verify locally
make hw-verify HW_VERSION=X.Y    # Recheck the bundle
```

Both commands are local and upload nothing. Review the files in
`hardware/output/release/physiclaw-hardware-vX.Y/`. If sources change, inspect
and move the old bundle aside, then prepare again.

Publishing requires a clean `main` checkout matching live `origin/main` and an
authenticated GitHub CLI (`gh`):

```sh
make hw-publish HW_VERSION=X.Y
```

Publishing deploys docs and waits for the new build and `docs.physiclaw.ai` to
serve the expected release marker, polling every 15 seconds for up to 15 minutes.
It then audits assets on `docs.physiclaw.ai` and `physiclaw.ai`. All asset checks
must pass; an existing production build serving the same release assets is
acceptable.

Keep the terminal open until verification finishes. Release rules are in
[AGENTS.md](AGENTS.md).

Logs and audit reports are saved under `hardware/output/site-asset-report/`.
If verification fails, the release remains published. Inspect Cloudflare after
a readiness timeout, then retry as needed:

```sh
make docs-deploy HW_VERSION=X.Y        # Redeploy docs, wait, and audit both sites
make check-site-assets HW_VERSION=X.Y  # Audit only
```

Omit `HW_VERSION` to check the newest published hardware release. For direct
Python use, check-only mode, and timeout options, see:

```sh
uv run --locked python scripts/docs_deploy.py --help
```
