"""PhysiClaw command-line interface."""

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
from physiclaw.cli.uninstall import uninstall

app = typer.Typer(
    help="PhysiClaw — let AI agents physically operate a phone.",
    context_settings={"help_option_names": ["-h", "--help"]},
    no_args_is_help=True,
    add_completion=False,
)

app.command()(doctor)
app.command()(server)
app.command()(status)
app.command()(uninstall)

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
