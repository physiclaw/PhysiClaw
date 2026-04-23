"""``physiclaw config`` — inspect and edit ``~/.physiclaw/config.toml``.

No ``set``/``unset`` — programmatic edits without losing comments and
ordering need a TOML round-trip lib (tomlkit) we'd rather not pull in
for a one-feature gain. Users edit by hand via ``physiclaw config edit``.
"""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Annotated

import typer

from physiclaw import config as _config

config_app = typer.Typer(
    help="Show or edit the user config file.",
    context_settings={"help_option_names": ["-h", "--help"]},
    no_args_is_help=True,
    add_completion=False,
)


def _load_or_exit(path: Path | None = None) -> _config.Config:
    try:
        return _config.load(path)
    except _config.ConfigError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(code=1)


@config_app.command("path")
def _path() -> None:
    """Print the config file path (whether or not it exists)."""
    typer.echo(_config.config_path())


@config_app.command("show")
def _show() -> None:
    """Dump the effective (merged) config as TOML."""
    typer.echo(_config.to_toml(_load_or_exit()), nl=False)


@config_app.command("get")
def _get(
    dotted: Annotated[
        str,
        typer.Argument(help="Section.key, e.g. engine.max_turns."),
    ],
) -> None:
    """Read one dotted key from the effective config."""
    cfg = _load_or_exit()
    try:
        val = _config.get(cfg, dotted)
    except _config.ConfigError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(code=1)
    if isinstance(val, bool):
        typer.echo("true" if val else "false")
    else:
        typer.echo(val)


@config_app.command("edit")
def _edit() -> None:
    """Open the config file in ``$EDITOR`` (creates it from the template if
    missing)."""
    path = _config.write_default()
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL")
    if not editor:
        for candidate in ("nano", "vim", "vi"):
            if shutil.which(candidate):
                editor = candidate
                break
    if not editor:
        typer.echo(
            f"no $EDITOR set and no nano/vim/vi on PATH. Edit by hand: {path}",
            err=True,
        )
        raise typer.Exit(code=1)
    subprocess.run([editor, str(path)], check=False)
    _load_or_exit(path)
    typer.echo(typer.style("✓ config OK", fg=typer.colors.GREEN))
    typer.echo("Restart `physiclaw server` to apply.")


__all__ = ["config_app"]
