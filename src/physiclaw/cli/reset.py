"""``physiclaw reset`` — clear calibration and the learned screen layout.

Deletes ``~/.physiclaw/calibration/`` (the arm/camera/screen calibration) and
``~/.physiclaw/screen-layout/`` (the input-box / keyboard / Paste bboxes learned
on first run). On the next run PhysiClaw re-calibrates and re-learns the layout —
so a new phone is set up cleanly from scratch. Memory, models, and config are
left untouched (use ``physiclaw uninstall`` for those).
"""

import shutil
from pathlib import Path
from typing import Annotated

import typer

from physiclaw.cli._format import exit_error
from physiclaw.common import paths


def _file_count(d: Path) -> int:
    return sum(1 for p in d.rglob("*") if p.is_file()) if d.exists() else 0


def reset(
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip the confirmation prompt."),
    ] = False,
) -> None:
    """Clear calibration and the learned screen layout so both are redone next run."""
    targets = [paths.calibration_dir(), paths.screen_layout_dir()]
    present: list[tuple[Path, int]] = []
    for directory in targets:
        count = _file_count(directory)
        if count:
            present.append((directory, count))
    if not present:
        typer.echo("Nothing to reset.")
        return

    typer.echo("Will clear:")
    for d, n in present:
        typer.echo(f"  {d}  ({n} file(s))")
    if not yes and not typer.confirm("Reset?", default=False):
        typer.echo("Cancelled.")
        raise typer.Exit(1)

    for d, _ in present:
        try:
            shutil.rmtree(d)
        except OSError as error:
            exit_error(f"could not remove {d}: {error}")
    typer.echo(
        "Cleared — PhysiClaw will re-calibrate and re-learn the layout on the next run."
    )
