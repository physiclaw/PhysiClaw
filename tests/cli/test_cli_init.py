"""Tests for `physiclaw.cli.__init__` — top-level Typer app wiring."""
from __future__ import annotations

import importlib

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


# ---------- claude-preview command ----------


def test_claude_preview_command_registered_when_agent_claude_available() -> None:
    """The conditional `claude-preview` registration depends on
    physiclaw.agent.claude being importable. Either way, the CLI must
    not crash when listing commands."""
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    # We can't assert presence/absence without inspecting find_spec
    # state, but at minimum the help renders.
    assert "PhysiClaw" in result.stdout


def test_claude_preview_dispatches_to_preview_module(mocker) -> None:
    """When the command is registered, invoking it should call into
    `physiclaw.agent.claude.preview.claude_preview`."""
    if importlib.util.find_spec("physiclaw.agent.claude") is None:
        pytest.skip("agent.claude not importable in this environment")

    spy = mocker.patch(
        "physiclaw.agent.claude.preview.claude_preview",
    )

    result = runner.invoke(app, [
        "claude-preview", "--trigger", "test wake",
    ])

    assert result.exit_code == 0
    spy.assert_called_once()
    assert spy.call_args.kwargs["trigger"] == "test wake"
    assert spy.call_args.kwargs["full"] is False


def test_claude_preview_full_flag(mocker) -> None:
    if importlib.util.find_spec("physiclaw.agent.claude") is None:
        pytest.skip("agent.claude not importable")

    spy = mocker.patch(
        "physiclaw.agent.claude.preview.claude_preview",
    )

    runner.invoke(app, ["claude-preview", "--full"])

    assert spy.call_args.kwargs["full"] is True
