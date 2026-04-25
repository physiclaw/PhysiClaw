"""``physiclaw doctor`` — read-only health check.

Server-aware: if a PhysiClaw server is running, the live ``/api/status``
response wins over local probes (the server holds the serial port and
camera, so re-probing them would either fail with "busy" or break the
server). When the server is offline, doctor actively probes the GRBL
arm and enumerates cameras.
"""

import logging
import platform
import shutil
import sys
from typing import Annotated

import typer

from physiclaw import __version__, paths, runtime_state
from physiclaw.cli._format import info as _fmt_info
from physiclaw.cli._format import ok as _fmt_ok
from physiclaw.cli._format import section as _fmt_section
from physiclaw.cli._format import warn as _fmt_warn


def _list_cameras(max_index: int = 4) -> list[int]:
    # Each failed index on macOS can block 1–3s in AVFoundation, so stop
    # after a couple of consecutive misses.
    import cv2

    from physiclaw.core.hardware.camera import silenced_stderr

    found: list[int] = []
    misses = 0
    with silenced_stderr():
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


def _probe_server(live: dict | None) -> tuple[str, int, bool, dict | None]:
    """GET /api/status from the server at ``live['host']:live['port']``,
    falling back to ``config.toml`` when no live state is passed.

    Returns ``(display_host, port, bind_all, status)``. ``bind_all`` is
    True when the configured host was the 0.0.0.0 wildcard — display_host
    is rewritten to "localhost" in that case so output reads cleanly,
    but the flag lets doctor surface the exposure warning separately.

    Caller-supplied ``live`` so doctor runs one ``read_live()`` per
    invocation — used twice (server probe + Provider section).
    """
    # Lazy: httpx import is ~100ms — paid only when doctor actually runs,
    # not on every `physiclaw --help`.
    import httpx

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

    from physiclaw.core.hardware.camera import silenced_stderr

    with silenced_stderr():
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
    # `complete` is a @property on `Calibration`, not a serialized field,
    # so `data.get("complete")` was always None — bundles that finished
    # the full setup were still reported as "partial". Reconstruct the
    # dataclass and check the live property instead.
    from physiclaw.core.calibration.state import Calibration

    data = paths.load_calibration_bundle()
    if data is None:
        return _fmt_warn("calibration: parse failed or not a JSON object")
    try:
        cal = Calibration.from_dict(data)
    except (TypeError, ValueError, KeyError) as e:
        return _fmt_warn(f"calibration: bundle unreadable — {e}")
    if cal.complete:
        return _fmt_ok("calibration: bundle complete")
    missing = [k for k, v in data.items() if v is None]
    return _fmt_warn(
        f"calibration: partial (missing: {missing})" if missing
        else f"calibration: partial (keys: {sorted(data.keys())})"
    )


def _probe_bridge_deep(host: str, port: int) -> str:
    import httpx  # cached after _probe_server's first import

    try:
        r = httpx.get(f"http://{host}:{port}/api/bridge/state", timeout=1.0)
        connected = r.json().get("connected", False)
    except (httpx.HTTPError, ValueError) as e:
        # Network/parse failure is a real problem — keep the warn.
        return _fmt_warn(f"bridge: {type(e).__name__}: {e}")
    if connected:
        return _fmt_ok("bridge: phone connected")
    # "not paired yet" is a transient state (phone hasn't opened /bridge),
    # not a broken configuration — surface the fact without `!`.
    return _fmt_info("bridge: phone not paired yet")


def _skills_lines() -> list[str]:
    """One formatted line per installed skill. CLI-installed skills carry
    a ``.installed-from`` JSON marker (see ``cli/skills.py``); user-authored
    dirs are valid too and render as ``(local)``."""
    from physiclaw.cli.skills import (
        PROVENANCE_FILE,
        installed_skill_dirs,
        read_provenance,
    )

    entries = installed_skill_dirs()
    if not entries:
        return [f"  (none installed in {paths.skills_dir()})"]
    out: list[str] = []
    for d in entries:
        prov = read_provenance(d)
        if prov is not None:
            ref = prov.get("ref") or (prov.get("sha", "") or "")[:7] or "?"
            out.append(_fmt_ok(f"{d.name}  ← {prov.get('source', '?')} @ {ref}"))
        elif (d / PROVENANCE_FILE).exists():
            # Marker present but unparseable — rare, worth flagging
            # separately from user-authored so the user can fix it.
            out.append(_fmt_warn(f"{d.name}: provenance unreadable"))
        else:
            out.append(_fmt_ok(f"{d.name}  (local)"))
    return out


def _probe_provider_chat_deep(provider_id: str, model_id: str) -> str:
    """Send a real `provider.chat()` round-trip — proves network, auth,
    billing, AND model response in one shot. Uses the same code path as
    a live wake (DTO history → provider.serialize_history → chat → parse).
    Reports model latency + token usage + a short reply preview so a bad
    response (e.g. tool-only output, content-filter, length-truncated)
    is visible inline."""
    import asyncio
    import time

    from physiclaw.agent.engine.dto import SystemMessage, UserMessage
    from physiclaw.agent.engine.trace import brief
    from physiclaw.agent.provider import make_provider

    try:
        prov = make_provider(provider_id, model_id)
    except (ValueError, RuntimeError) as e:
        return _fmt_warn(f"{provider_id}/{model_id} api: setup — {e}")

    history = [
        SystemMessage(content="You are a one-word reply bot. Reply with exactly one word."),
        UserMessage(content="Reply with the word 'pong'."),
    ]

    async def _run():
        try:
            return await prov.chat(history, tools=[])
        finally:
            await prov.aclose()

    t0 = time.perf_counter()
    try:
        asst = asyncio.run(_run())
    except Exception as e:
        # Provider raised — could be transport (network), auth (401/403),
        # billing (402 / vendor-specific), or rate limit (429). Surface the
        # message so the user sees which.
        return _fmt_warn(
            f"{provider_id}/{model_id} api: {type(e).__name__}: {brief(str(e), 120)}"
        )
    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    reply = (asst.content or "").strip().replace("\n", " ")
    if not reply and not asst.tool_calls:
        return _fmt_warn(
            f"{provider_id}/{model_id} api: empty reply (finish={asst.finish_reason}, "
            f"{elapsed_ms}ms)"
        )
    preview = brief(reply, 40) if reply else f"<{len(asst.tool_calls)} tool_calls>"
    u = asst.usage
    usage_str = (
        f"{u.prompt_tokens}p+{u.completion_tokens}c"
        if u.prompt_tokens else "no usage reported"
    )
    return _fmt_ok(
        f"{provider_id}/{model_id} api: reply={preview!r} "
        f"({elapsed_ms}ms, {usage_str})"
    )


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

    typer.echo(_fmt_section("PhysiClaw doctor"))
    typer.echo(f"  physiclaw:  {__version__}")
    typer.echo(f"  python:     {sys.version.split()[0]} ({sys.executable})")
    typer.echo(
        f"  platform:   {platform.system()} {platform.release()} "
        f"({platform.machine()})"
    )
    uv = shutil.which("uv")
    typer.echo(f"  uv:         {uv or '(not found — not required for runtime)'}")

    typer.echo()
    typer.echo(_fmt_section("Paths"))
    typer.echo(f"  home: {paths.HOME}")

    typer.echo()
    typer.echo(_fmt_section("Config"))
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
    typer.echo(_fmt_section("Assets"))
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
    typer.echo(_fmt_section("Hardware"))
    # One read_live() per doctor run — reused by _probe_server and the
    # Provider section below.
    live = runtime_state.read_live()
    host, port, bind_all, status = _probe_server(live)
    if status is not None:
        # Server is up — its view of arm/camera/calibration is authoritative.
        typer.echo(_fmt_ok(f"server: running on {host}:{port}"))
        if bind_all:
            # Bind mode is a config decision the user already made; surface
            # the fact without the yellow `!` since a healthy server isn't
            # a warning state. Security notes live in the README.
            typer.echo(_fmt_info("bind: 0.0.0.0 (LAN-reachable; intended for the phone bridge)"))
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
            typer.echo(_fmt_ok(f"cameras: {len(cams)} detected"))
            if deep:
                for idx in cams:
                    typer.echo(_probe_camera_frame(idx))
        else:
            typer.echo(_fmt_warn(
                "cameras: none detected. On first use, macOS shows a "
                "Camera-permission prompt — accept it for this terminal app."
            ))

    typer.echo()
    typer.echo(_fmt_section("Calibration"))
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
    typer.echo(_fmt_section("Engine"))

    # Prefer the live server's recorded choice — resolving here would pull
    # from the current shell's env, which may be missing PHYSICLAW_MODEL
    # even when the server has it set. Fall back to a fresh resolve when no
    # server is running.
    from physiclaw.agent.runtime.launcher import engine_label, resolve
    from physiclaw.config import parse_model_ref

    live_ref = live.get("model_ref") if live else None
    active_ref: str | None = None
    if live_ref:
        active_ref = live_ref
        ref_source = f"live server, {live.get('model_source') or '?'}"
        typer.echo(_fmt_ok(f"{engine_label(active_ref)} ({ref_source})"))
    else:
        try:
            active_ref, ref_source = resolve()
            typer.echo(_fmt_ok(f"{engine_label(active_ref)} (from {ref_source})"))
        except RuntimeError as e:
            typer.echo(_fmt_warn(f"engine: invalid — {e}"))

    active_provider, active_model = (None, None)
    if active_ref:
        try:
            active_provider, active_model = parse_model_ref(active_ref)
        except ValueError:
            pass

    # Surface the active provider's key status. In `--deep` mode, send a
    # real `chat()` round-trip on the active provider (proves network +
    # auth + billing + model response).
    from physiclaw.agent.provider import CLAUDE_CODE_ID, provider_key_status
    if active_provider and active_provider != CLAUDE_CODE_ID:
        masked, source = provider_key_status(active_provider)
        if masked is None:
            typer.echo(_fmt_warn(
                f"{active_provider} api key: (unset) — required for the active model. "
                f"Set via `physiclaw models key {active_provider}` or env var."
            ))
        else:
            typer.echo(_fmt_ok(f"{active_provider} api key: {masked} ({source})"))
            if deep and active_model:
                typer.echo(_probe_provider_chat_deep(active_provider, active_model))

    typer.echo()
    typer.echo(_fmt_section("Skills"))
    for line in _skills_lines():
        typer.echo(line)

    typer.echo()
    typer.echo(_fmt_section("Next steps"))
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
