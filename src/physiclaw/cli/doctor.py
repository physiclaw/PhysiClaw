"""``physiclaw doctor`` — health check and environment diagnostics."""

import os
import platform
import shutil
import sys
from typing import Annotated

import typer

from physiclaw import __version__, paths


def _fmt_ok(msg: str) -> str:
    return typer.style("✓ ", fg=typer.colors.GREEN) + msg


def _fmt_warn(msg: str) -> str:
    return typer.style("! ", fg=typer.colors.YELLOW) + msg


def _list_serial_ports() -> list[str]:
    from serial.tools.list_ports import comports

    return [p.device for p in comports()]


def _list_cameras(max_index: int = 4) -> list[int]:
    # Each failed index on macOS can block 1–3s in AVFoundation, so stop
    # after a couple of consecutive misses.
    import cv2

    found: list[int] = []
    misses = 0
    for i in range(max_index + 1):
        cap = cv2.VideoCapture(i)
        if cap.isOpened():
            found.append(i)
            misses = 0
        else:
            misses += 1
        cap.release()
        if misses >= 2:
            break
    return found


def doctor(
    fix: Annotated[
        bool,
        typer.Option("--fix", help="Attempt to auto-repair any issues found."),
    ] = False,
) -> None:
    """Check environment, hardware, and assets. Report what's missing."""
    typer.echo(typer.style("PhysiClaw doctor", bold=True))
    typer.echo(f"  physiclaw:  {__version__}")
    typer.echo(f"  python:     {sys.version.split()[0]} ({sys.executable})")
    typer.echo(
        f"  platform:   {platform.system()} {platform.release()} "
        f"({platform.machine()})"
    )
    uv = shutil.which("uv")
    typer.echo(f"  uv:         {uv or '(not found — not required for runtime)'}")

    typer.echo()
    typer.echo(typer.style("Paths", bold=True))
    for name, p in (
        ("DATA_DIR  ", paths.DATA_DIR),
        ("CACHE_DIR ", paths.CACHE_DIR),
        ("CONFIG_DIR", paths.CONFIG_DIR),
        ("LOG_DIR   ", paths.LOG_DIR),
    ):
        exists = p.exists()
        typer.echo(f"  {name}  {p}  {'' if exists else '(missing)'}")

    if fix:
        paths.ensure_dirs()
        typer.echo(_fmt_ok("created user dirs"))

    typer.echo()
    typer.echo(typer.style("Assets", bold=True))
    model = paths.omniparser_onnx()
    if model.exists():
        size_mb = model.stat().st_size / 1024 / 1024
        typer.echo(_fmt_ok(f"vision model: {model}  ({size_mb:.1f} MB)"))
    else:
        typer.echo(_fmt_warn(
            f"vision model missing: {model}\n"
            "    Run: physiclaw setup local-vision-model"
        ))

    typer.echo()
    typer.echo(typer.style("Hardware", bold=True))
    from physiclaw.core.hardware.grbl import _LIKELY_KEYWORDS, _SKIP_KEYWORDS

    all_ports = _list_serial_ports()
    likely = [
        p for p in all_ports
        if any(kw in p.lower() for kw in _LIKELY_KEYWORDS)
        and not any(kw in p.lower() for kw in _SKIP_KEYWORDS)
    ]
    if likely:
        typer.echo(_fmt_ok(f"USB serial candidates: {', '.join(likely)}"))
    elif all_ports:
        typer.echo(_fmt_warn(
            f"no likely GRBL ports, but these serial ports are present: "
            f"{', '.join(all_ports)}"
        ))
    else:
        typer.echo(_fmt_warn(
            "no USB serial ports detected — connect the arm and re-run. "
            "If your board uses CH340/CP210x, you may need a driver."
        ))

    cams = _list_cameras()
    if cams:
        typer.echo(_fmt_ok(f"cameras at indices: {cams}"))
    else:
        typer.echo(_fmt_warn(
            "no cameras detected. On first use, macOS shows a "
            "Camera-permission prompt — accept it for this terminal app."
        ))

    typer.echo()
    typer.echo(typer.style("Calibration", bold=True))
    bundle = paths.calibration_bundle()
    if bundle.exists():
        typer.echo(_fmt_ok(f"calibration bundle: {bundle}"))
    else:
        typer.echo(_fmt_warn(
            f"no calibration yet: {bundle}\n"
            "    Run: physiclaw setup hardware (starts the server + guides you)"
        ))

    typer.echo()
    typer.echo(typer.style("Environment", bold=True))
    for var in (
        "PHYSICLAW_PROVIDER",
        "QWEN_API_KEY",
        "KIMI_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    ):
        typer.echo(f"  {var:20s} {'set' if var in os.environ else '(unset)'}")

    typer.echo()
    typer.echo(typer.style("Next steps", bold=True))
    if not model.exists():
        typer.echo("  1. physiclaw setup local-vision-model")
    if not bundle.exists():
        typer.echo("  2. physiclaw setup hardware")
    typer.echo("  3. physiclaw server")
