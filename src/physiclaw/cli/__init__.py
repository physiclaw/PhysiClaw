"""PhysiClaw command-line interface."""

from importlib.util import find_spec
from typing import Annotated

import typer

from physiclaw import __version__ as _pkg_version
from physiclaw.cli.config import config_app
from physiclaw.cli.doctor import doctor
from physiclaw.cli.models import models_app
from physiclaw.cli.server import server
from physiclaw.cli.setup import setup_app
from physiclaw.cli.skills import skills_app
from physiclaw.cli.status import status

app = typer.Typer(
    help="PhysiClaw — let AI agents physically operate a phone.",
    context_settings={"help_option_names": ["-h", "--help"]},
    no_args_is_help=True,
    add_completion=False,
)

app.command()(doctor)
app.command()(server)
app.command()(status)

# Claude-code-specific commands — registered only when agent/claude/ is
# importable. The body defers the heavy import (spawn.py → engine.skill
# → mcp_inventory → …) until the command actually runs, so
# `physiclaw --help` stays fast.
if find_spec("physiclaw.agent.claude") is not None:

    @app.command(name="claude-preview")
    def _claude_preview(
        trigger: Annotated[
            str,
            typer.Option(
                "--trigger", "-t",
                help="Synthetic trigger description to preview against.",
            ),
        ] = "manual preview wake",
        full: Annotated[
            bool,
            typer.Option("--full", help="Print the full system prompt and full argv."),
        ] = False,
    ) -> None:
        """Assemble the `claude -p` command for a synthetic trigger without spawning."""
        from physiclaw.agent.claude.preview import claude_preview

        claude_preview(trigger=trigger, full=full)
app.add_typer(
    setup_app,
    name="setup",
    help="One-time configuration: hardware, local models, phone.",
)
app.add_typer(
    config_app,
    name="config",
    help="Show or edit the user config file.",
)
app.add_typer(
    models_app,
    name="models",
    help="List, inspect, and switch the active model.",
)
app.add_typer(
    skills_app,
    name="skills",
    help="Install, list, and remove skills from a git-repo source.",
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"physiclaw, version {_pkg_version}")
        raise typer.Exit()


@app.callback()
def _root(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    pass


__all__ = ["app"]
