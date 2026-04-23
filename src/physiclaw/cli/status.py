"""``physiclaw status`` — quick snapshot (compare to `doctor` which probes deeper)."""

import json

import typer

from physiclaw import paths


def status() -> None:
    """Show calibration + model state at a glance. Doesn't probe hardware."""
    typer.echo(typer.style("PhysiClaw status", bold=True))

    model = paths.omniparser_onnx()
    if model.exists():
        size_mb = model.stat().st_size / 1024 / 1024
        typer.echo(
            typer.style("  vision model  ", fg=typer.colors.GREEN)
            + f"ok  ({size_mb:.0f} MB)"
        )
    else:
        typer.echo(
            typer.style("  vision model  ", fg=typer.colors.YELLOW) + "missing"
        )

    bundle = paths.calibration_bundle()
    if bundle.exists():
        try:
            data = json.loads(bundle.read_text())
            complete = data.get("complete", False)
        except (json.JSONDecodeError, OSError):
            complete = False
        tag = "complete" if complete else "partial"
        color = typer.colors.GREEN if complete else typer.colors.YELLOW
        typer.echo(typer.style("  calibration   ", fg=color) + tag)
    else:
        typer.echo(
            typer.style("  calibration   ", fg=typer.colors.YELLOW) + "missing"
        )

    jobs = paths.jobs_file()
    if jobs.exists():
        typer.echo(
            typer.style("  jobs file     ", fg=typer.colors.GREEN) + str(jobs)
        )
    else:
        typer.echo(
            typer.style("  jobs file     ", fg=typer.colors.YELLOW)
            + f"none yet ({jobs})"
        )
