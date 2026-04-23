"""PhysiClaw command-line interface.

Subcommands mirror openclaw's shape where meaningful (`doctor`, `onboard`,
`status`, `setup`) so users familiar with that CLI can adapt quickly.
"""

from typing import Annotated

import typer

from physiclaw import __version__ as _pkg_version
from physiclaw.cli.doctor import doctor
from physiclaw.cli.onboard import onboard
from physiclaw.cli.server import server
from physiclaw.cli.setup import setup_app
from physiclaw.cli.status import status

app = typer.Typer(
    help="PhysiClaw — let AI agents physically operate a phone.",
    context_settings={"help_option_names": ["-h", "--help"]},
    no_args_is_help=True,
    add_completion=False,
)

app.command()(doctor)
app.command()(onboard)
app.command()(server)
app.command()(status)
app.add_typer(
    setup_app,
    name="setup",
    help="One-time configuration: hardware, local models, phone.",
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
