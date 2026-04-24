"""``physiclaw doctor`` — read-only health check.

Server-aware: if a PhysiClaw server is running, the live ``/api/status``
response wins over local probes (the server holds the serial port and
camera, so re-probing them would either fail with "busy" or break the
server). When the server is offline, doctor actively probes the GRBL
arm and enumerates cameras.
"""

import contextlib
import logging
import os
import platform
import shutil
import sys
from typing import Annotated

import httpx
import typer

from physiclaw import __version__, paths, runtime_state


def _fmt_ok(msg: str) -> str:
    return typer.style("✓ ", fg=typer.colors.GREEN) + msg


def _fmt_warn(msg: str) -> str:
    return typer.style("! ", fg=typer.colors.YELLOW) + msg


@contextlib.contextmanager
def _silenced_stderr():
    """Redirect OS-level stderr (fd 2) to /dev/null for the block.

    OpenCV/AVFoundation/ffmpeg print "out device of bound" and similar
    via C-level fprintf, bypassing Python's logging — only an fd-level
    redirect catches them.
    """
    sys.stderr.flush()
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        saved = os.dup(2)
        try:
            os.dup2(devnull, 2)
            yield
        finally:
            os.dup2(saved, 2)
            os.close(saved)
    finally:
        os.close(devnull)


def _list_cameras(max_index: int = 4) -> list[int]:
    # Each failed index on macOS can block 1–3s in AVFoundation, so stop
    # after a couple of consecutive misses.
    import cv2

    found: list[int] = []
    misses = 0
    with _silenced_stderr():
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


def _probe_server() -> tuple[str, int, bool, dict | None]:
    """Find the live server (or guess from config) and GET /api/status.

    Returns ``(display_host, port, bind_all, status)``. ``bind_all`` is
    True when the configured host was the 0.0.0.0 wildcard — display_host
    is rewritten to "localhost" in that case so output reads cleanly,
    but the flag lets doctor surface the exposure warning separately.
    """
    live = runtime_state.read_live()
    if live:
        host, port = live["host"], live["port"]
    else:
        from physiclaw.config import CONFIG

        host, port = CONFIG.server.host, CONFIG.server.port
    bind_all = host == "0.0.0.0"
    if bind_all:
        host = "localhost"
    try:
        r = httpx.get(f"http://{host}:{port}/api/status", timeout=1.0)
        return (host, port, bind_all, r.json())
    except (httpx.HTTPError, ValueError):
        return (host, port, bind_all, None)


# --- Deep probes (only run with --deep). ------------------------------------
# Each returns a pre-formatted line ready for typer.echo. Failures are
# non-fatal — doctor reports them and moves on.


def _probe_vision_model_deep() -> str:
    try:
        import onnxruntime as ort

        sess = ort.InferenceSession(
            str(paths.omniparser_onnx()), providers=["CPUExecutionProvider"]
        )
        shape = sess.get_inputs()[0].shape
        return _fmt_ok(f"vision model: loads OK (input {shape})")
    except (OSError, RuntimeError, ValueError, ImportError) as e:
        return _fmt_warn(f"vision model: load failed — {type(e).__name__}: {e}")


def _probe_camera_frame(index: int) -> str:
    """cap.read() catches the macOS-TCC-denied case (isOpened lies)."""
    import cv2

    with _silenced_stderr():
        cap = cv2.VideoCapture(index)
        try:
            for _ in range(3):
                ok, frame = cap.read()
                if ok and frame is not None:
                    h, w = frame.shape[:2]
                    return _fmt_ok(f"camera {index}: frame OK ({w}x{h})")
            return _fmt_warn(
                f"camera {index}: opens but no frame (likely denied Camera "
                "permission — System Settings → Privacy & Security)"
            )
        finally:
            cap.release()


def _probe_calibration_deep() -> str:
    data = paths.load_calibration_bundle()
    if data is None:
        return _fmt_warn("calibration: parse failed or not a JSON object")
    if data.get("complete"):
        return _fmt_ok("calibration: bundle complete")
    return _fmt_warn(f"calibration: partial (keys: {sorted(data.keys())})")


def _probe_bridge_deep(host: str, port: int) -> str:
    try:
        r = httpx.get(f"http://{host}:{port}/api/bridge/state", timeout=1.0)
        connected = r.json().get("connected", False)
    except (httpx.HTTPError, ValueError) as e:
        return _fmt_warn(f"bridge: {type(e).__name__}: {e}")
    return (_fmt_ok if connected else _fmt_warn)(
        f"bridge: phone {'connected' if connected else 'not paired yet'}"
    )


def _probe_qwen_api_deep() -> str:
    """1-token completion to confirm the key actually works (not just present)."""
    from physiclaw.agent.engine.provider import provider_endpoint
    from physiclaw.config import qwen_api_key

    key = qwen_api_key()
    if not key:
        return _fmt_warn("qwen api: skipped (no key)")
    base_url, model = provider_endpoint("qwen")
    try:
        r = httpx.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1,
            },
            timeout=10.0,
        )
    except httpx.HTTPError as e:
        return _fmt_warn(f"qwen api: {type(e).__name__}: {e}")
    if r.status_code == 200:
        return _fmt_ok(f"qwen api: {r.status_code} OK ({model})")
    return _fmt_warn(f"qwen api: HTTP {r.status_code}: {r.text[:80]}")


def doctor(
    deep: Annotated[
        bool,
        typer.Option(
            "--deep",
            help="Run active probes (model load, camera frame, API call). "
            "May take several seconds.",
        ),
    ] = False,
) -> None:
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
        if deep:
            typer.echo(_probe_vision_model_deep())
    else:
        typer.echo(_fmt_warn(
            f"vision model missing: {model}\n"
            "    Run: physiclaw setup local-vision-model"
        ))

    typer.echo()
    typer.echo(typer.style("Hardware", bold=True))
    host, port, bind_all, status = _probe_server()
    if status is not None:
        # Server is up — its view of arm/camera/calibration is authoritative.
        typer.echo(_fmt_ok(f"server: running on {host}:{port}"))
        if bind_all:
            typer.echo(_fmt_warn(
                "bind: 0.0.0.0 — required for the phone bridge. Use only "
                "on a private LAN. Do not run on a public-IP host."
            ))
        for label in ("arm", "camera", "calibrated", "ready"):
            if status.get(label):
                typer.echo(_fmt_ok(f"{label}: yes"))
            else:
                typer.echo(_fmt_warn(f"{label}: no"))
        if deep:
            typer.echo(_probe_bridge_deep(host, port))
    else:
        # Server offline — safe to probe hardware ourselves.
        typer.echo(_fmt_warn("server: not running"))
        typer.echo("  Probing serial ports for GRBL (active $I query, ~2s/port)…")
        from physiclaw.core.hardware.grbl import candidate_ports, detect_grbl

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
            relevant = candidate_ports()
            if relevant:
                typer.echo(_fmt_warn(
                    f"no GRBL detected (saw {len(relevant)} candidate port(s): "
                    f"{', '.join(relevant)})"
                ))
            else:
                typer.echo(_fmt_warn(
                    "no serial ports detected — connect the arm and re-run."
                ))
        cams = _list_cameras()
        if cams:
            typer.echo(_fmt_ok(f"cameras: {len(cams)} detected (indices {cams})"))
            if deep:
                for idx in cams:
                    typer.echo(_probe_camera_frame(idx))
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
        if deep:
            typer.echo(_probe_calibration_deep())
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
        if deep:
            typer.echo(_probe_qwen_api_deep())
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
