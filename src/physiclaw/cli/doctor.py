"""``physiclaw doctor`` — read-only health check.

Server-aware: if a PhysiClaw server is running, the live ``/api/status``
response wins over local probes (the server holds the serial port and
camera, so re-probing them would either fail with "busy" or break the
server). When the server is offline, doctor actively probes the GRBL
arm and enumerates cameras.
"""

import logging
import os
import platform
import shutil
import sys

import httpx
import typer

from physiclaw import __version__, paths, runtime_state


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


def _probe_server() -> tuple[str, int, dict | None]:
    """Find the live server (or guess from config) and GET /api/status.

    Returns ``(host, port, status_dict_or_None)``. Any httpx/JSON failure
    → status None. Matches the runtime's simple httpx-with-timeout pattern.
    """
    live = runtime_state.read_live()
    if live:
        host, port = live["host"], live["port"]
    else:
        from physiclaw.config import CONFIG

        host, port = CONFIG.server.host, CONFIG.server.port
    connect_host = "127.0.0.1" if host == "0.0.0.0" else host
    try:
        r = httpx.get(f"http://{connect_host}:{port}/api/status", timeout=1.0)
        return (host, port, r.json())
    except (httpx.HTTPError, ValueError):
        return (host, port, None)


def doctor() -> None:
    """Check environment, hardware, and assets. Report what's missing."""
    paths.ensure_dirs()

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
    typer.echo(f"  data: {paths.HOME}")

    typer.echo()
    typer.echo(typer.style("Config", bold=True))
    # If config.toml failed to parse, importing physiclaw.config at the top
    # of this module would have raised — so reaching here means the file is
    # either absent or valid.
    from physiclaw import config as _cfg

    cp = _cfg.config_path()
    if cp.exists():
        typer.echo(_fmt_ok(f"config.toml: {cp}"))
    else:
        typer.echo(_fmt_warn(
            f"no config yet — using built-in defaults. Edit via `physiclaw config edit` ({cp})"
        ))

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
    host, port, status = _probe_server()
    if status is not None:
        # Server is up — its view of arm/camera/calibration is authoritative.
        typer.echo(_fmt_ok(f"server: running on {host}:{port}"))
        for label in ("arm", "camera", "calibrated", "ready"):
            if status.get(label):
                typer.echo(_fmt_ok(f"{label}: yes"))
            else:
                typer.echo(_fmt_warn(f"{label}: no"))
    else:
        # Server offline — safe to probe hardware ourselves.
        typer.echo(_fmt_warn("server: not running"))
        typer.echo("  Probing serial ports for GRBL (active $I query, ~2s/port)…")
        from physiclaw.core.hardware.grbl import detect_grbl

        # detect_grbl logs its own narration; doctor speaks for itself.
        grbl_logger = logging.getLogger("physiclaw.core.hardware.grbl")
        prev_level = grbl_logger.level
        grbl_logger.setLevel(logging.CRITICAL)
        try:
            grbl_port = detect_grbl()
        finally:
            grbl_logger.setLevel(prev_level)
        if grbl_port:
            typer.echo(_fmt_ok(f"GRBL arm: {grbl_port}"))
        else:
            all_ports = _list_serial_ports()
            if all_ports:
                typer.echo(_fmt_warn(
                    f"no GRBL detected (saw {len(all_ports)} serial port(s): "
                    f"{', '.join(all_ports)})"
                ))
            else:
                typer.echo(_fmt_warn(
                    "no serial ports detected — connect the arm and re-run."
                ))
        cams = _list_cameras()
        if cams:
            typer.echo(_fmt_ok(f"cameras: {len(cams)} detected (indices {cams})"))
        else:
            typer.echo(_fmt_warn(
                "cameras: none detected. On first use, macOS shows a "
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
            "    Run: physiclaw setup hardware (needs the server running)"
        ))

    typer.echo()
    typer.echo(typer.style("Provider", bold=True))

    from physiclaw.agent.runtime.launcher import resolve

    try:
        provider_choice, provider_source = resolve()
        typer.echo(_fmt_ok(f"provider: {provider_choice} (from {provider_source})"))
    except RuntimeError as e:
        provider_choice = None
        typer.echo(_fmt_warn(f"provider: invalid — {e}"))

    # Only show keys that map to a wired provider today (just qwen).
    qwen_src = _cfg.qwen_api_key_source()
    if qwen_src:
        typer.echo(_fmt_ok(f"qwen api key: set ({qwen_src})"))
    elif provider_choice == "qwen":
        typer.echo(_fmt_warn("qwen api key: (unset) — required for provider=qwen"))

    typer.echo()
    typer.echo(typer.style("Next steps", bold=True))
    steps = []
    if not model.exists():
        steps.append("physiclaw setup local-vision-model")
    if status is None:
        steps.append("physiclaw server   (leave running in one shell)")
    if not (status and status.get("ready")):
        steps.append("physiclaw setup hardware   (in another shell — talks to the server)")
    for i, step in enumerate(steps, 1):
        typer.echo(f"  {i}. {step}")
    if not steps:
        typer.echo("  All set.")
