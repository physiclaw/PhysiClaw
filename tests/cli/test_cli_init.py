"""Tests for `physiclaw.cli.__init__` — top-level Typer app wiring."""
from __future__ import annotations

import pytest
from typer.testing import CliRunner

import physiclaw
from physiclaw.cli import app

runner = CliRunner()


# ---------- root commands wired ----------


@pytest.mark.parametrize(
    "subcommand",
    ["doctor", "server", "status", "setup", "config", "models", "skills"],
)
def test_top_level_help_lists_subcommand(subcommand: str) -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert subcommand in result.stdout


# ---------- --version ----------


def test_version_flag_prints_pkg_version() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert physiclaw.__version__ in result.stdout
