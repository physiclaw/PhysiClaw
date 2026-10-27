.PHONY: test test-cov test-fast test-slow test-integration test-all test-studio-ui mutate lint fmt typecheck help firmware bump build publish release docs-deploy \
        hw-help hw-parts hw-build hw-check hw-step hw-print hw-manual hw-manual-pdf hw-sourcing hw-url hw-drawing hw-mark hw-replay hw-camera hw-rebuild hw-preflight hw-prepare hw-verify hw-publish hw-release _hw-package

PY ?= uv run
PYTHON ?= $(PY) python

# Hardware CAD pipeline (see hardware/README.md). Geometry stages need the
# `cad` group; the manual / sourcing / drawing builders are standard-library only.
# Pass flags through ARGS, e.g.  make hw-build ARGS="--bom --bom-delta"
HW     = $(PY) --locked --group cad python -m hardware
HW_DOC = $(PY) --locked python -m hardware

# Passed as data through the environment, never interpolated into a shell command.
export HW_VERSION

# Guard for hw-* targets that need a positional: fail with a usage hint when
# ARGS is empty. $(1) describes the expected value.
need-args = $(if $(strip $(ARGS)),,$(error usage: make $@ ARGS=$(1)))

# Release options are passed as data, never interpolated into shell commands.
export VERSION VERBOSE RELEASE_FLOW

help:
	@echo "Targets:"
	@echo "  test                  — fast unit suite (default; excludes slow + integration)"
	@echo "  test-cov              — fast suite with coverage report (term + html)"
	@echo "  test-fast             — alias for test"
	@echo "  test-slow             — only @pytest.mark.slow"
	@echo "  test-integration      — only @pytest.mark.integration"
	@echo "  test-all              — every test, including slow and integration"
	@echo "  test-studio-ui        — JavaScript UI regressions (Node 22+)"
	@echo "  mutate MOD=path       — mutmut on a path (e.g. MOD=src/physiclaw/agent/engine/validator.py)"
	@echo "  lint                  — ruff check + format check"
	@echo "  fmt                   — ruff format (src, tests, hardware, firmware, scripts)"
	@echo "  typecheck             — mypy on application and hardware port (scope in [tool.mypy])"
	@echo "  firmware [ARGS=...]    — rebuild fluidnc.zip, preserve output/.cache; ARGS=--help for options"
	@echo "  bump [VERSION=X.Y.Z]  — bump version. Defaults to incrementing the last"
	@echo "                          component split by '.' (0.0.7 → 0.0.8). Override with"
	@echo "                          VERSION for major/minor jumps. Commits LOCALLY."
	@echo "  build                 — uv build wheel + sdist for the version in pyproject.toml"
	@echo "  publish               — upload current wheel + sdist to PyPI, tag, push. Irreversible."
	@echo "                          Reads version from pyproject.toml. Needs UV_PUBLISH_TOKEN."
	@echo "  release [VERSION=X.Y.Z]"
	@echo "                        — full release: bump + build + publish in one shot."
	@echo "                          VERBOSE=1 shows command logs; NO_COLOR=1 disables color."
	@echo ""
	@$(MAKE) --no-print-directory hw-help

test:
	$(PY) pytest

test-cov:
	$(PY) pytest --cov=src/physiclaw --cov-report=term-missing --cov-report=html --cov-branch

test-fast: test

test-slow:
	$(PY) pytest -m slow

test-integration:
	$(PY) pytest -m integration

test-all:
	$(PY) pytest -m ""

test-studio-ui:
	node --test tests/studio/test_studio_ui.cjs tests/agent/test_viewer_ui.cjs tests/core/test_setup_hardware_ui.cjs

mutate:
	@if [ -z "$(MOD)" ]; then echo "usage: make mutate MOD=src/physiclaw/<path>"; exit 2; fi
	$(PY) mutmut run --paths-to-mutate $(MOD)

lint:
	$(PY) ruff check src/ tests/ firmware/ hardware/port/ hardware/release.py hardware/build_lock.py hardware/assembly/cache.py hardware/assembly/build_procedures.py
	$(PY) ruff format --check src/ tests/ hardware/ firmware/ scripts/

fmt:
	$(PY) ruff format src/ tests/ hardware/ firmware/ scripts/

# Scope and settings live in [tool.mypy] in pyproject.toml.
typecheck:
	$(PY) mypy

# Standalone PEP 723 script; uv supplies the image build dependencies.
# Example: make firmware ARGS="--tag v4.1.0 --out /tmp/fluidnc.zip"
firmware:
	@$(PY) firmware/firmware.py $(ARGS)

# --- Release workflow ---------------------------------------------------------
#
# Three atoms; only the last one is irreversible:
#
#   make bump VERSION=0.0.8   # edits pyproject + uv.lock, commits LOCALLY
#   make build                # uv build (reads version from pyproject)
#   make publish              # uv publish, git tag vX.Y.Z, git push. Irreversible.
#
# Up to (and including) `build`, everything is reversible:
#   git reset --hard HEAD~        # undo the bump commit
#   rm dist/physiclaw-0.0.8*      # discard the build
#
# `publish` reads the version from pyproject.toml so you only type it once
# (in `bump`). Tag creation happens in `publish` — that way a failed build /
# abandoned attempt doesn't leave a stale local tag for a version that was
# never released.

# `make release [VERSION=X.Y.Z]` — bump + build + publish in one shot.
#
# Make reserves `-p` (print database), so a `make bump -p` style custom
# flag isn't possible — this meta-target is the equivalent.
#
# The stdlib driver works with cmd.exe and POSIX shells. Skip project discovery
# so starting a release cannot sync dependencies before checking the working tree.
# One process keeps the full release sequential even under make -j.
bump build publish release:
	@$(PY) --no-project python scripts/release.py $@

# --- Hardware CAD pipeline ----------------------------------------------------
# Thin wrappers over `python -m hardware <subcommand>` (see `make hw-help` and
# hardware/README.md). hw-prefixed so `build` stays the wheel build above.
# Flags / positionals go through ARGS, e.g.  make hw-build ARGS="--bom".

hw-help:
	@echo "Hardware — run from the repository root:"
	@echo ""
	@echo "  Local work (no upload):"
	@echo "    make hw-build [ARGS=--bom]           Build CAD drawings and STEPs; reuse cache"
	@echo "    make hw-check                       Check hardware source consistency"
	@echo "    make hw-url                         Show 3 Taobao and 3 AliExpress URLs"
	@echo "    make hw-prepare HW_VERSION=X.Y       Build and verify a local release bundle; reuse cache"
	@echo "    make hw-verify HW_VERSION=X.Y        Verify an existing bundle without rebuilding"
	@echo ""
	@echo "  Release (commit and push main first):"
	@echo "    make hw-release HW_VERSION=X.Y       Prepare → verify → publish → trigger docs deploy"
	@echo "    make hw-publish HW_VERSION=X.Y       Publish a prepared bundle → trigger docs deploy"
	@echo "    make hw-preflight HW_VERSION=X.Y     Check Git/GitHub readiness; no upload"
	@echo "    make docs-deploy                     Trigger only the docs hook; use if that step failed"
	@echo ""
	@echo '  Build or edit one output (flags through ARGS="..."):'
	@echo "    make hw-step ARGS=<stem>             Build one assembly step with its BOM"
	@echo "    make hw-parts                       Export part STEPs"
	@echo "    make hw-print                       Package custom parts for 3D printing"
	@echo "    make hw-manual                      Build bilingual manual HTML"
	@echo "    make hw-manual-pdf                   Build bilingual manual HTML and PDFs"
	@echo "    make hw-sourcing                    Build bilingual sourcing guides"
	@echo "    make hw-drawing                     Build extrusion drawings as HTML and PDFs"
	@echo "    make hw-mark ARGS=<svg|json>         Open the drawing annotation tool"
	@echo "    make hw-replay [ARGS=file]           Apply saved drawing annotations"
	@echo "    pbpaste | make hw-camera            Convert a FreeCAD view to a Camera() literal"
	@echo ""
	@echo "  Force rebuild:"
	@echo "    make hw-rebuild [ARGS=--bom]         Same as hw-build; bypass CAD cache"
	@echo ""
	@echo "  Options: make hw-build ARGS=--help (also works for other build commands)"
	@echo "  Guide: hardware/README.md"

hw-parts:
	@$(HW) parts $(ARGS)

hw-build:
	@$(HW) build $(ARGS)

# Static cross-artifact consistency check (stems / patches / manual /
# sourcing) — stdlib-only, no cad group, also run in CI.
hw-check:
	@$(HW_DOC) check $(ARGS)

hw-step:
	$(call need-args,<procedure_stem>)
	@$(HW) step $(ARGS)

hw-print:
	@$(HW) print $(ARGS)

hw-manual:
	@$(HW_DOC) manual $(ARGS)

# Convenience alias for `make hw-manual ARGS="--pdf"`; extra flags still go
# through ARGS, e.g.  make hw-manual-pdf ARGS="--lang en".
hw-manual-pdf:
	@$(HW_DOC) manual --pdf $(ARGS)

hw-sourcing:
	@$(HW_DOC) sourcing $(ARGS)

hw-url:
	@$(HW_DOC) url $(ARGS)

# The supplier's sheet for the frame extrusions — drawn from the same
# constants the assembly cuts and drills with, so it can't drift from the
# model. PDFs print through headless Chrome like the manual.
hw-drawing:
	@$(HW_DOC) drawing --pdf $(ARGS)

hw-mark:
	$(call need-args,<svg|json>)
	@$(HW) mark $(ARGS)

hw-replay:
	@$(HW) replay $(ARGS)

# No need-args guard: `camera` also reads the view from stdin when ARGS is
# empty (projection.py), so `pbpaste | make hw-camera` works too.
hw-camera:
	@$(HW) camera $(ARGS)

# Same assembly build and flags as hw-build, bypassing persistent cache reads.
hw-rebuild:
	@$(HW) build --no-cache $(ARGS)

# All release entry points delegate to the same guarded Python workflow.
# Each is a single recipe, so make -j cannot reorder preflight/build/publish.
# hw-prepare and hw-verify are local; hw-release and hw-publish publish.
hw-preflight:
	@$(HW_DOC) release preflight $(ARGS)

hw-prepare:
	@$(HW) release prepare $(ARGS)

hw-verify:
	@$(HW_DOC) release verify $(ARGS)

hw-release:
	@$(HW) release deploy $(ARGS)

hw-publish:
	@$(HW_DOC) release publish $(ARGS)

# Keep the legacy internal target on the guarded publication path.
_hw-package: hw-publish

# Redeploy docs.physiclaw.ai after a hardware release: the site pulls the
# manual, sourcing guide and drawing from the latest release at build time.
# The Cloudflare Pages deploy hook URL is a secret (anyone holding it can
# trigger a deploy), so it lives in the gitignored .docs-deploy-hook, one line.
docs-deploy:
	@$(PYTHON) "$(dir $(abspath $(lastword $(MAKEFILE_LIST))))scripts/docs_deploy.py"
