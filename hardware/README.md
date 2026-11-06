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

Install dependencies and generate the assembly models and drawings first:

```sh
uv sync --locked --group cad
make hw-build          # Generate assembly models and drawings
```

Then generate the documents and custom-parts package you need. The manual uses
the drawings from the previous step.

```sh
make hw-manual-pdf     # Build the assembly manual (HTML + PDF)
make hw-sourcing       # Build the sourcing guide (HTML)
make hw-drawing        # Build frame cut-and-drill sheets (HTML + PDF)
make hw-print          # Package custom parts for 3D printing
```

Documents include English and Chinese editions. By default, generated files go to
`hardware/output/`, in `step/`, `svg/`, `manual/`, `sourcing/`, `drawing/` and
`print_3d/`. Builds reuse cached CAD work automatically.

| Task | Command |
| --- | --- |
| Build one assembly step | `make hw-step ARGS=belt_20_clamp` |
| Include per-step parts lists | `make hw-build ARGS=--bom` |
| Use less memory | `make hw-build ARGS="--jobs 1"` |
| See all commands | `make hw-help` |

Without Make, use the Python CLI, including in PowerShell:

```sh
uv run --locked --group cad python -m hardware build
uv run --locked python -m hardware manual --pdf
uv run --locked python -m hardware --help
```

## Directory layout

```text
hardware/
├── parts/                 Part models
│   ├── standard/          Off-the-shelf parts
│   └── custom/            Custom parts
├── assembly/
│   ├── procedures/        Assembly steps
│   ├── patch/             Saved drawing annotations
│   └── mark/              Drawing annotation tool
├── documents/
│   ├── bom/bom.json       Shared purchasing list
│   ├── manual/            Assembly instructions and manual builder
│   ├── sourcing/          Suppliers and sourcing guide builder
│   ├── drawing/           Frame cut-and-drill drawings
│   └── assets/            Shared icons and illustrations
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
uv run --locked --group cad pytest tests/hardware
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

Publishing also triggers the docs site deployment. If that hook fails, retry
`make docs-deploy`; the release is already published. The site updates when its
asynchronous build finishes. Release rules are in [AGENTS.md](AGENTS.md).
