.PHONY: test test-cov test-fast test-slow test-integration test-all mutate lint help bump build publish release

PY ?= uv run

# Version currently declared in pyproject.toml — used by `build` and `publish`
# so the user only types it once (in `make bump`).
PKG_VERSION = $(shell sed -n 's/^version = "\(.*\)"$$/\1/p' pyproject.toml)

help:
	@echo "Targets:"
	@echo "  test                  — fast unit suite (default; excludes slow + integration)"
	@echo "  test-cov              — fast suite with coverage report (term + html)"
	@echo "  test-fast             — alias for test"
	@echo "  test-slow             — only @pytest.mark.slow"
	@echo "  test-integration      — only @pytest.mark.integration"
	@echo "  test-all              — every test, including slow and integration"
	@echo "  mutate MOD=path       — mutmut on a path (e.g. MOD=src/physiclaw/agent/engine/validator.py)"
	@echo "  lint                  — ruff check"
	@echo "  bump [VERSION=X.Y.Z]  — bump version. Defaults to incrementing the last"
	@echo "                          component split by '.' (0.0.7 → 0.0.8). Override with"
	@echo "                          VERSION for major/minor jumps. Commits LOCALLY."
	@echo "  build                 — uv build wheel + sdist for the version in pyproject.toml"
	@echo "  publish               — upload dist/* to PyPI, tag vX.Y.Z, push. Irreversible."
	@echo "                          Reads version from pyproject.toml. Needs UV_PUBLISH_TOKEN."
	@echo "  release [VERSION=X.Y.Z]"
	@echo "                        — full release: bump + build + publish in one shot."

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

mutate:
	@if [ -z "$(MOD)" ]; then echo "usage: make mutate MOD=src/physiclaw/<path>"; exit 2; fi
	$(PY) mutmut run --paths-to-mutate $(MOD)

lint:
	$(PY) ruff check src/ tests/

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

bump:
	@if [ -n "$$(git status --porcelain)" ]; then \
		echo "✗ working tree dirty — commit or stash first"; exit 1; fi
	@if [ "$$(git rev-parse --abbrev-ref HEAD)" != "main" ]; then \
		echo "✗ not on main branch"; exit 1; fi
	@set -e; \
	if [ -n "$(VERSION)" ]; then \
		NEW="$(VERSION)"; \
	else \
		LAST="$$(echo $(PKG_VERSION) | awk -F. '{print $$NF}')"; \
		case "$$LAST" in *[!0-9]*|"") \
			echo "✗ can't auto-bump non-numeric last component '$$LAST' of $(PKG_VERSION)"; \
			echo "  use 'make bump VERSION=X.Y.Z' explicitly"; exit 1;; \
		esac; \
		NEW="$$(echo $(PKG_VERSION) | awk 'BEGIN{FS=OFS="."} {$$NF = $$NF + 1; print}')"; \
		echo "Auto-incrementing $(PKG_VERSION) → $$NEW"; \
	fi; \
	if git rev-parse "v$$NEW" >/dev/null 2>&1; then \
		echo "✗ tag v$$NEW already exists"; exit 1; fi; \
	sed -i.bak "s/^version = \"[^\"]*\"$$/version = \"$$NEW\"/" pyproject.toml; \
	rm pyproject.toml.bak; \
	uv lock; \
	git add pyproject.toml uv.lock; \
	git commit -m "chore: bump version to $$NEW"; \
	printf '\n\033[32m✓\033[0m Bumped to %s.\n' "$$NEW"; \
	printf 'Next:  make build  &&  make publish\n\n'

build:
	@if [ -z "$(PKG_VERSION)" ]; then \
		echo "✗ couldn't read version from pyproject.toml"; exit 1; fi
	@rm -f dist/physiclaw-$(PKG_VERSION)*
	uv build
	@printf '\n\033[32m✓\033[0m Built $(PKG_VERSION):\n'
	@echo "  dist/physiclaw-$(PKG_VERSION)-py3-none-any.whl"
	@echo "  dist/physiclaw-$(PKG_VERSION).tar.gz"

publish:
	@if [ -z "$(PKG_VERSION)" ]; then \
		echo "✗ couldn't read version from pyproject.toml"; exit 1; fi
	@if [ -z "$$UV_PUBLISH_TOKEN" ]; then \
		echo "✗ UV_PUBLISH_TOKEN not set"; \
		echo "  Create a PyPI API token: https://pypi.org/manage/account/token/"; \
		echo "  Then: export UV_PUBLISH_TOKEN=pypi-AgEI..."; \
		exit 1; fi
	@if [ ! -f "dist/physiclaw-$(PKG_VERSION)-py3-none-any.whl" ]; then \
		echo "✗ dist/physiclaw-$(PKG_VERSION)-py3-none-any.whl not found"; \
		echo "  Run 'make build' first"; exit 1; fi
	@if git rev-parse "v$(PKG_VERSION)" >/dev/null 2>&1; then \
		echo "✗ tag v$(PKG_VERSION) already exists — was this version already published?"; \
		exit 1; fi
	@printf 'Uploading to PyPI:\n'
	@printf '  dist/physiclaw-$(PKG_VERSION)-py3-none-any.whl\n'
	@printf '  dist/physiclaw-$(PKG_VERSION).tar.gz\n'
	@COUNT=$$(ls dist/physiclaw-*.whl 2>/dev/null | grep -vc "physiclaw-$(PKG_VERSION)-py3" || true); \
	 if [ "$$COUNT" -gt 0 ]; then \
		printf '  (%s older wheels in dist/ ignored)\n' "$$COUNT"; \
	 fi
	@printf '\n'
	uv publish dist/physiclaw-$(PKG_VERSION)-py3-none-any.whl dist/physiclaw-$(PKG_VERSION).tar.gz
	git tag -a v$(PKG_VERSION) -m "physiclaw v$(PKG_VERSION)"
	git push origin main
	git push origin v$(PKG_VERSION)
	@printf '\n\033[32m✓\033[0m Published $(PKG_VERSION) to PyPI and pushed to GitHub.\n'
	@echo "  https://pypi.org/project/physiclaw/$(PKG_VERSION)/"

# `make release [VERSION=X.Y.Z]` — bump + build + publish in one shot.
#
# Make reserves `-p` (print database), so a `make bump -p` style custom
# flag isn't possible — this meta-target is the equivalent.
#
# Make runs prerequisites left-to-right (no -j); the deferred-eval
# PKG_VERSION re-reads pyproject.toml at recipe time, so build and
# publish pick up the freshly-bumped version that bump just wrote.
release: bump build publish
	@printf '\n\033[32m✓\033[0m Release flow complete for $(PKG_VERSION).\n'
