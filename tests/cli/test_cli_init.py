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
    [
        "doctor",
        "server",
        "now",
        "auto",
        "mcp",
        "testdrive",
        "test-assistive-touch",
        "status",
        "setup",
        "config",
        "models",
        "skills",
    ],
)
def test_top_level_help_lists_subcommand(subcommand: str) -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert subcommand in result.stdout


# ---------- --version ----------


@pytest.mark.parametrize("flag", ["--version", "-v"])
def test_version_flag_prints_pkg_version(flag: str) -> None:
    result = runner.invoke(app, [flag])

    assert result.exit_code == 0
    assert result.stdout.strip() == f"{physiclaw.__version__} (PhysiClaw)"


def test_help_header_includes_pkg_version() -> None:
    """``physiclaw --help`` should show the version in the description line —
    matches the uv / cargo / npm convention so users can sanity-check what
    they have installed."""
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert physiclaw.__version__ in result.stdout


# ---------- bare invocation defaults to `server` ----------


def test_no_subcommand_runs_server(mocker) -> None:
    """Bare ``physiclaw`` (no subcommand) defaults to ``physiclaw server``."""
    mock_server = mocker.patch("physiclaw.cli.server")

    result = runner.invoke(app, [])

    assert result.exit_code == 0
    mock_server.assert_called_once_with()


@pytest.mark.parametrize("args", [[], ["server"], ["now"], ["auto"], ["mcp"]])
def test_invalid_config_blocks_startup_instead_of_using_defaults(
    physiclaw_home, mocker, args
):
    (physiclaw_home / "config.toml").write_text("[server]\nportt = 9000\n")
    server = mocker.patch("physiclaw.cli.server")
    result = runner.invoke(app, args)

    assert result.exit_code == 1
    assert "portt" in result.stderr
    assert "config edit" in result.stderr
    server.assert_not_called()


@pytest.mark.parametrize(
    "args", [["--help"], ["--version"], ["config", "path"], ["update"]]
)
def test_invalid_config_keeps_recovery_commands_available(physiclaw_home, args):
    (physiclaw_home / "config.toml").write_text("[server]\nportt = 9000\n")
    assert runner.invoke(app, args).exit_code == 0
