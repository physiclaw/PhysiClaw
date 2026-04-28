.PHONY: test test-cov test-fast test-slow test-integration test-all mutate lint help

PY ?= uv run

help:
	@echo "Targets:"
	@echo "  test             — fast unit suite (default; excludes slow + integration)"
	@echo "  test-cov         — fast suite with coverage report (term + html)"
	@echo "  test-fast        — alias for test"
	@echo "  test-slow        — only @pytest.mark.slow"
	@echo "  test-integration — only @pytest.mark.integration"
	@echo "  test-all         — every test, including slow and integration"
	@echo "  mutate MOD=path  — mutmut on a path (e.g. MOD=src/physiclaw/agent/engine/validator.py)"
	@echo "  lint             — ruff check"

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
