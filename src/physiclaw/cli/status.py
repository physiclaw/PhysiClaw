"""``physiclaw status`` — quick snapshot (compare to `doctor` which probes deeper)."""

import sys
from os import stat_result
from pathlib import Path
from stat import S_ISREG

import typer

from physiclaw.cli._format import next_hint, section
from physiclaw.cli._update_check import maybe_print_update_banner
from physiclaw.common import paths


def _file_info(path: Path) -> tuple[stat_result | None, str | None]:
    """Read regular-file metadata once; missing files have neither info nor error."""
    try:
        info = path.stat()
    except FileNotFoundError:
        return None, None
    except OSError as e:
        return None, f"unavailable ({e})"
    if not S_ISREG(info.st_mode):
        return None, "not a file"
    return info, None


def _calibration_status() -> str:
    try:
        data = paths.load_calibration_bundle()
    except OSError as e:
        return f"unavailable ({e})"
    if data is None:
        return "missing"

    # `complete` is computed, never serialized. Import lazily: calibration
    # pulls cv2/numpy, too heavy for this module's import time.
    from physiclaw.core.calibration.state import Calibration

    try:
        complete = Calibration.from_dict(data).complete
    except (TypeError, ValueError, KeyError):
        return "partial"  # doctor carries the deeper diagnosis
    return "complete" if complete else "partial"


def _row(label: str, detail: str, ready: bool = False) -> None:
    color = typer.colors.GREEN if ready else typer.colors.YELLOW
    typer.echo(typer.style(f"  {label:<14}", fg=color) + detail)


def status() -> None:
    """Show calibration + model state at a glance. Doesn't probe hardware."""
    typer.echo(section("PhysiClaw status"))

    model_info, model_error = _file_info(paths.omniparser_onnx())
    if model_info is not None:
        size_mb = model_info.st_size / 1024 / 1024
        _row("vision model", f"ok  ({size_mb:.0f} MB)", ready=True)
    else:
        _row("vision model", model_error or "missing")

    calibration = _calibration_status()
    _row("calibration", calibration, ready=calibration == "complete")

    jobs = paths.jobs_file()
    jobs_info, jobs_error = _file_info(jobs)
    if jobs_info is not None:
        _row("jobs file", str(jobs), ready=True)
    else:
        _row("jobs file", f"{jobs_error or 'none yet'} ({jobs})")

    # Suppress on pipe — `status` is a quick snapshot meant for grep/jq.
    if sys.stdout.isatty():
        typer.echo()
        typer.echo(
            next_hint(
                "physiclaw doctor  (for deeper checks — server, hardware, provider)"
            )
        )

    maybe_print_update_banner()
