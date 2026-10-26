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
import urllib.error
from collections.abc import Iterator
from typing import Annotated

import typer

from physiclaw import __version__
from physiclaw.cli import _http
from physiclaw.cli._doctor_report import Check, DoctorReport
from physiclaw.cli._doctor_report import error as _fmt_error
from physiclaw.cli._doctor_report import info as _fmt_info
from physiclaw.cli._doctor_report import ok as _fmt_ok
from physiclaw.cli._doctor_report import warn as _fmt_warn
from physiclaw.cli._update_check import maybe_print_update_banner
from physiclaw.common import paths, runtime_state
from physiclaw.common.ready import STATUS_PATH, ready_from_status


def _opencv_import_error() -> Check | None:
    """Return a diagnostic if ``import cv2`` fails, else None.

    The platform backend augments with OS-specific remediation (e.g. the
    libGL/glib system libs the manylinux wheel needs on minimal Linux).
    """
    try:
        import cv2  # noqa: F401
    except ImportError as e:
        from physiclaw import port as os_platform

        extra = os_platform.opencv_import_hint(e)
        details = (str(e), extra.strip()) if extra else (str(e),)
        return _fmt_error("Camera library", "OpenCV could not load", *details)
    return None


def _list_cameras(max_index: int = 4) -> list[int]:
    # Each failed index on macOS can block 1–3s in AVFoundation, so stop
    # after a couple of consecutive misses.
    import cv2

    from physiclaw import port as os_platform
    from physiclaw.core.hardware.camera import silenced_stderr

    found: list[int] = []
    misses = 0
    backend = os_platform.camera_backend()
    with silenced_stderr():
        for i in range(max_index + 1):
            cap = cv2.VideoCapture(i, backend)
            try:
                if cap.isOpened():
                    found.append(i)
                    misses = 0
                else:
                    misses += 1
            finally:
                cap.release()
            if misses >= 2:
                break
    return found


def _offline_hardware_lines(deep: bool) -> Iterator[Check]:
    """Probe devices only after the caller has ruled out a live server.

    Yield the progress line before touching hardware; isolate serial and
    camera failures so one unavailable device does not hide later checks.
    """
    from physiclaw import port as os_platform
    from physiclaw.core.hardware.grbl import candidate_ports, detect_grbl

    yield _fmt_info("Server", "Not running", "Checking connected hardware directly.")
    yield _fmt_info("USB scan", "Checking for the arm; about 2 seconds per port.")
    grbl_logger = logging.getLogger("physiclaw.core.hardware.grbl")
    prev_level = grbl_logger.level
    grbl_logger.setLevel(logging.CRITICAL)
    try:
        try:
            grbl_port = detect_grbl()
        finally:
            grbl_logger.setLevel(prev_level)
        if grbl_port:
            yield _fmt_ok("Arm", "Detected", grbl_port)
        else:
            relevant = candidate_ports()
            if relevant:
                yield _fmt_warn(
                    "Arm",
                    "No GRBL controller responded",
                    f"Ports checked: {', '.join(relevant)}",
                    "Check the USB connection and the control board's 12V power.",
                )
            else:
                yield _fmt_warn(
                    "Arm",
                    "No serial ports detected",
                    "Connect the control board by USB and turn on its 12V power.",
                )
    except Exception as e:
        yield _fmt_error("Arm", "Connection check failed", f"{type(e).__name__}: {e}")

    cv2_err = _opencv_import_error()
    if cv2_err is not None:
        yield cv2_err
    else:
        try:
            cams = _list_cameras()
        except Exception as e:
            yield _fmt_error("Cameras", "Detection failed", f"{type(e).__name__}: {e}")
        else:
            if cams:
                yield _fmt_ok("Cameras", f"{len(cams)} detected")
                if deep:
                    for idx in cams:
                        try:
                            yield _probe_camera_frame(idx)
                        except Exception as e:
                            yield _fmt_error(
                                f"Camera {idx}",
                                "Frame check failed",
                                f"{type(e).__name__}: {e}",
                            )
            else:
                yield _fmt_warn(
                    "Cameras", "None detected", os_platform.camera_denied_hint()
                )
    for hint in os_platform.hardware_permission_hints():
        yield _fmt_warn("Permissions", hint)


def _probe_server(live: dict | None) -> tuple[str, int, bool, object]:
    """GET /api/status from the server at ``live['host']:live['port']``,
    falling back to ``config.toml`` when no live state is passed.

    Returns ``(display_host, port, bind_all, status)``. ``bind_all`` is
    True when the configured host was the 0.0.0.0 wildcard — display_host
    is rewritten to "localhost" in that case so output reads cleanly,
    but the flag lets doctor surface the exposure warning separately.

    Caller-supplied ``live`` so doctor runs one ``read_live()`` per
    invocation — used twice (server probe + Provider section).
    """
    if live:
        host, port = live["host"], live["port"]
    else:
        from physiclaw.common.config import CONFIG

        host, port = CONFIG.server.host, CONFIG.server.port
    from physiclaw.common.config import WILDCARD_HOSTS, url_host

    # Both wildcards ("0.0.0.0" AND "::"): a `--host ::` server probed at
    # http://:::PORT read as offline, and doctor's offline branch then
    # opens serial/cameras the live server holds.
    bind_all = host in WILDCARD_HOSTS
    if bind_all:
        host = "localhost"
    # _http.api handles the proxy-bypass policy and fails soft to None.
    status = _http.api(
        f"http://{url_host(host)}:{port}", "GET", STATUS_PATH, timeout=1.0
    )
    return (host, port, bind_all, status)


# --- Deep probes (only run with --deep). ------------------------------------
# Each returns a check for the report. Failures are
# non-fatal — doctor reports them and moves on.


def _probe_vision_model_deep() -> Check:
    try:
        import onnxruntime as ort

        sess = ort.InferenceSession(
            str(paths.omniparser_onnx()), providers=["CPUExecutionProvider"]
        )
        shape = sess.get_inputs()[0].shape
        return _fmt_ok("Model load", "Loaded successfully", f"Input shape: {shape}")
    except Exception as e:
        # ONNX Runtime's native errors (e.g. InvalidProtobuf) do not share
        # a narrower base class. A failed model must not abort diagnostics.
        return _fmt_error(
            "Model load", "Could not load the vision model", f"{type(e).__name__}: {e}"
        )


def _probe_camera_frame(index: int) -> Check:
    """cap.read() catches the macOS-TCC-denied case (isOpened lies).

    Reproduces the server's real capture negotiation (backend + FOURCC +
    size + exposure via `configure_capture`) so the frame it meters is
    the frame sessions will see, then judges it with `quality.assess` —
    catching a blown/blurry rig at doctor time instead of mid-task."""
    import cv2

    from physiclaw import port as os_platform
    from physiclaw.common.config import CONFIG
    from physiclaw.core.hardware.camera import configure_capture, silenced_stderr

    with silenced_stderr():
        cap = cv2.VideoCapture(index, os_platform.camera_backend())
        try:
            configure_capture(
                cap,
                exposure_auto=CONFIG.camera.auto_exposure,
                exposure=CONFIG.camera.exposure,
            )
            frame = None
            # ~10 reads: the first frames are pre-auto-exposure garbage.
            for _ in range(10):
                ok, got = cap.read()
                if ok and got is not None:
                    frame = got
            if frame is None:
                return _fmt_error(
                    f"Camera {index}",
                    "Opened, but no frame received",
                    os_platform.camera_denied_hint(),
                )
        finally:
            cap.release()
    h, w = frame.shape[:2]
    good, clause = _frame_quality_verdict(frame)
    if good:
        return _fmt_ok(f"Camera {index}", f"Frame captured ({w}x{h})", clause)
    issue, _, metrics = clause.partition("\n")
    return _fmt_warn(f"Camera {index}", issue, f"Frame captured ({w}x{h})", metrics)


def _frame_quality_verdict(frame) -> tuple[bool, str]:
    """One quality clause for the camera line, cropped to the phone screen
    when the calibration bundle allows it (rotated-frame size must match
    the bundle's cam_size aspect — a same-aspect size difference is a
    renegotiated capture mode and rescales cleanly, mirroring the
    server's warm-start reconcile; an aspect change breaks the crop)."""
    import cv2

    from physiclaw.core.calibration.state import Calibration
    from physiclaw.core.vision import quality
    from physiclaw.core.vision.preprocess import crop_to_phone_screen

    scope = "whole frame; no calibration crop"
    cal = Calibration.load()  # None on missing/unreadable bundle, logged
    # The cam_size check restates what transforms_ready already implies;
    # it narrows the Optional for the unpack below.
    if cal is not None and cal.transforms_ready and cal.cam_size is not None:
        if cal.effective_rotation() != -1:
            frame = cv2.rotate(frame, cal.effective_rotation())
        h, w = frame.shape[:2]
        cal_w, cal_h = cal.cam_size
        if cal.reconcile_cam_size((w, h)):
            frame = crop_to_phone_screen(frame, cal.transforms())
            scope = "phone screen"
            if (w, h) != (cal_w, cal_h):
                scope += f" (calibrated {cal_w}x{cal_h}, rescaled to {w}x{h})"
        else:
            scope = (
                f"whole frame; aspect ratio changed from {cal_w}x{cal_h} "
                f"to {w}x{h}; recalibrate"
            )
    r = quality.assess(frame)
    stats = (
        f"Sharpness {r.sharpness:.0f}, clipped pixels {r.clip_pct:.0%}, "
        f"median brightness {r.median_luma:.0f}, bright regions {r.white_blobs}. "
        f"Measured: {scope}."
    )
    if r.blown:
        return False, f"Overexposed. Reduce exposure or glare.\n{stats}"
    if r.dark:
        return False, f"Too dark. Check lighting and exposure.\n{stats}"
    if r.blurry:
        return False, f"Blurry. Adjust the camera focus.\n{stats}"
    return True, f"Image quality passed.\n{stats}"


def _probe_calibration_deep() -> Check:
    # `complete` is a @property on `Calibration`, not a serialized field,
    # so `data.get("complete")` was always None — bundles that finished
    # the full setup were still reported as "partial". Reconstruct the
    # dataclass and check the live property instead.
    from physiclaw.core.calibration.state import Calibration

    data = paths.load_calibration_bundle()
    if data is None:
        return _fmt_error("Calibration data", "Could not read a valid JSON object")
    try:
        cal = Calibration.from_dict(data)
    except (TypeError, ValueError, KeyError) as e:
        return _fmt_error("Calibration data", "Could not read calibration", str(e))
    if cal.complete:
        return _fmt_ok("Calibration data", "Complete")
    missing = [k for k, v in data.items() if v is None]
    return _fmt_warn(
        "Calibration data",
        "Incomplete",
        f"Missing values: {', '.join(missing)}"
        if missing
        else "Finish the hardware setup wizard.",
    )


def _probe_bridge_deep(host: str, port: int) -> Check:
    """``port`` is the control port; the bridge listens on port+1 (see
    ``core/bridge/lan.bridge_port``). The bridge plane is ungated, so a
    plain loopback GET works here."""
    from physiclaw.common.config import url_host
    from physiclaw.core.bridge.lan import bridge_port

    try:
        try:
            state = _http.fetch_json(
                f"http://{url_host(host)}:{bridge_port(port)}/api/bridge/state",
                timeout=1.0,
            )
        except urllib.error.HTTPError as error:
            error.close()
            raise
        if not isinstance(state, dict):
            raise ValueError("invalid bridge state: expected a JSON object")
        connected = state.get("connected", False)
        if not isinstance(connected, bool):
            raise ValueError("invalid bridge state: connected must be a boolean")
    except (OSError, ValueError) as e:
        # Network/parse failures are errors, unlike an unpaired phone.
        return _fmt_error(
            "Phone bridge", "Connection check failed", f"{type(e).__name__}: {e}"
        )
    if connected:
        return _fmt_ok("Phone bridge", "Phone connected")
    # An unpaired phone is an informational state, not a failed check.
    return _fmt_info("Phone bridge", "Phone not paired")


def _skills_lines() -> list[Check]:
    """One check per installed skill. CLI-installed skills carry
    a ``.installed-from`` JSON marker (see ``cli/skills.py``); user-authored
    directories are reported as local skills."""
    from physiclaw.cli.skills import (
        PROVENANCE_FILE,
        installed_skill_dirs,
        read_provenance,
    )

    entries = installed_skill_dirs()
    if not entries:
        return [
            _fmt_info("Skills", "None installed (optional)", str(paths.skills_dir()))
        ]
    out: list[Check] = []
    for d in entries:
        prov = read_provenance(d)
        if isinstance(prov, dict) and all(
            prov.get(key) is None or isinstance(prov[key], str)
            for key in ("source", "ref", "sha")
        ):
            ref = (
                prov.get("ref") or (prov.get("sha", "") or "")[:7] or "revision unknown"
            )
            out.append(
                _fmt_ok(
                    d.name,
                    "Installed",
                    f"{prov.get('source') or 'Source unknown'} · {ref}",
                )
            )
        elif (d / PROVENANCE_FILE).exists():
            # Marker present but unparseable — rare, worth flagging
            # separately from user-authored so the user can fix it.
            out.append(_fmt_warn(d.name, "Install source could not be read"))
        else:
            out.append(_fmt_ok(d.name, "Local skill"))
    return out


# 96x24 PNG, "PhysiClaw" black-on-white in 14pt Arial Bold. ~1.1 KB.
# The probe asks the model to read the word back — confirms not just
# that the wire accepts image_url, but that the model can actually see
# (and OCR) what it received.
_PROBE_IMAGE_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAGAAAAAYCAIAAACKi2/DAAAEQ0lEQVR42u2Y2yt0exjHv2stLcyi"
    "aCQih2kKpZkmkVvmDpFElEYM01zIP+BwxYUYSU5RbswIFw45xIU7KXKDnJXcGKdmxiGnMfPsi197"
    "7dnjfXdv70vZmu/d+v2eeQ6f+f2e9bQ4IkJAPxcfQBAAFAAUABQA9P8BFBQUxHEcx3E8zwcHB6tU"
    "qp6eHrbFcVxUVNSHRPVzdXBwUFFRER0dLYpiSkpKe3u7x+P58KC/Kfq3BEF4bzM5OcmmAaVSSR+t"
    "jY2N8PBwv4g1NTVs95OC/rp+DOj+/t7r9drt9tLSUgCVlZVyrisrK2q1WpKkuro6t9tNRHq9HsDC"
    "wgLzUF9fD2BoaIiIRkZGUlNTQ0JCoqOjDQbD7e3t+7K1Wi2A/Pz8o6Mjl8vV1NTEGO3t7flZLi4u"
    "6nS60NDQsLAwvV5/fHxMRBkZGYIguFwuOXRLSwsRHR4eAsjNzf0sQB6P5/LysrCw0BdQSEiI779t"
    "s9mIaHh4GIDJZGIekpKSBEG4vr7e3NzkOM73XBgMBj9A+/v7ACRJcjqdcg55eXnd3d1PT0++lna7"
    "PTg42NdbXl4eETU3NwOYmZkhIo1GA0Cv1xNRf38/AIvF8imA/DQ/Py8P3G1tbQ6Hw2g0AmhoaCAi"
    "h8MhimJsbKzX693e3pZTHB8fB2A0Gl9fX/2j/l327OwsAJ1O99P83l0xr9e7u7sLQK1WE9Ha2hrL"
    "xOl08jzPcZwkSW63u6SkhHW3zwLEcZxCodBqtaOjo3Kuoii+vb0R0eTkJIDa2lq2VVBQAGB9fb2t"
    "rQ3A4OAgEd3c3CQnJ7MK2aG4v7/3K3t6ehqAVqv9FUB3d3dWq9VsNqelpQGIj48nIo/Ho1Qq09PT"
    "5+bmABQVFQHY2NhQKpUqleoTe9B/58oKMxqN7NFmswFobGzMzs4WBOHy8pKtu1yuoaGh8vLypKQk"
    "ABkZGX6utra2ACgUCofDIQcqKSnp6Oh4fHz0tTw/P1er1Tqdbnx8/PT0lOf5hIQEZl9RUQGgqqqK"
    "47j19XV2l+UD/iUAPTw8KBSKxMREnudzcnLe/9Zut0dERABgfVp25fV6U1NTARQUFJycnNzd3bW2"
    "trLzu7u762tpsVgAVFdX397eDgwMAEhMTGTOR0dHAQiCoNFoiCgmJoZVsby8/FUAEVFZWRm7m/39"
    "/Wylr6/Pr5fJvcbX1erqqkKh8LM0m81+lmNjY/8MbzwfFhYme7i6uuJ5HkB9fT0RsTevJEnPz89f"
    "CBBb4Xn+4uJCXuzu7k5LS2Ov+dLS0rOzsx+23p2dneLi4sjIyNDQ0PT09K6uLo/H42f59vZmNpsj"
    "IiLi4uKsVmtxcTGAo6MjZpaVlQVgYmKCiHp7ewEUFhZ+yhz021paWgKQmZlJ30tBfz6LPz09ud3u"
    "zs5OAPJF+z76c8ZTU1OiKEZGRppMppeXl292grjAJ9fA544AoACgAKCvq78Atz3+gKHHygsAAAAA"
    "SUVORK5CYII="
)


_PROBE_IMAGE_WORD = "PhysiClaw"


def _engine_lines(live: dict | None, *, deep: bool) -> Iterator[Check]:
    """Report the live engine selection, or resolve locally when offline."""
    if live and live.get("no_runtime") is True:
        yield _fmt_info(
            "Runtime",
            "External MCP client",
            "Built-in runtime disabled; no provider key required here.",
        )
        return
    if live and live.get("model_error"):
        yield _fmt_error(
            "Model",
            "Invalid configuration on the live server",
            str(live["model_error"]),
            action=("physiclaw config edit", "Correct the model configuration."),
        )
        return

    from physiclaw.agent.runtime.launcher import resolve
    from physiclaw.provider import provider_key_status, validate_model_ref

    # A running server's environment may differ from this shell's.
    live_ref = live.get("model_ref") if live else None
    live_src = live.get("model_source") if live else None
    try:
        if live_ref:
            active_ref = live_ref
            ref_source = f"live server, {live_src or 'source unknown'}"
        else:
            active_ref, ref_source = resolve()
        if not isinstance(active_ref, str):
            raise ValueError("model ref must be a string")
        provider, model = validate_model_ref(active_ref)
    except (RuntimeError, ValueError) as e:
        yield _fmt_error(
            "Model",
            "Invalid configuration",
            str(e),
            action=(
                "physiclaw config edit",
                "Set a valid provider/model in agent.model.",
            ),
        )
        return

    yield _fmt_ok("Model", active_ref, f"Source: {ref_source}")
    masked, source = provider_key_status(provider)
    if masked is None:
        yield _fmt_warn(
            "API key",
            f"Missing for {provider}",
            "Required for the selected model.",
            action=(
                f"physiclaw models key {provider}",
                f"Add your {provider} API key.",
            ),
        )
        return

    yield _fmt_ok("API key", f"Configured for {provider}", f"Source: {source}")
    if deep:
        yield _probe_provider_deep(provider, model)


def _probe_provider_deep(provider_id: str, model_id: str) -> Check:
    """One round-trip with text + a tiny inline image — exercises the
    full path PhysiClaw needs (network, auth, billing, vision input,
    text output). PhysiClaw requires vision on every peek, so a text-
    only probe wouldn't add coverage.

    The probe asks the model to read the word in the image (`PhysiClaw`)
    and validates the reply contains it. "Non-empty reply" alone isn't
    enough: some endpoints (e.g. DashScope's compat shim for text-only
    Qwen models) accept image_url parts and silently drop them server-
    side, then hallucinate an answer. Demanding the actual word back
    catches that case.
    """
    import asyncio
    import time

    from physiclaw.agent.trace import brief
    from physiclaw.contract.dto import (
        ImageBlock,
        Message,
        SystemMessage,
        TextBlock,
        UserMessage,
    )
    from physiclaw.provider import make_chat_provider

    try:
        prov = make_chat_provider(provider_id, model_id)
    except (ValueError, RuntimeError) as e:
        return _fmt_error(
            "API test", "Provider setup failed", f"{provider_id}/{model_id}: {e}"
        )

    history: list[Message] = [
        SystemMessage(content="You are a one-word reply bot."),
        UserMessage(
            content=[
                TextBlock(text="What word is in this image? Reply with just the word."),
                ImageBlock(media_type="image/png", data_b64=_PROBE_IMAGE_B64),
            ]
        ),
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
        return _fmt_error(
            "API test",
            "Request failed",
            f"{provider_id}/{model_id}: {type(e).__name__}: {brief(str(e), 140)}",
        )
    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    reply = (asst.content or "").strip().replace("\n", " ")
    if not reply:
        return _fmt_error(
            "API test",
            "Model returned an empty reply",
            f"{provider_id}/{model_id} · {elapsed_ms} ms · Finish reason: {asst.finish_reason}",
        )
    if _PROBE_IMAGE_WORD.lower() not in reply.lower():
        return _fmt_error(
            "API test",
            "Image recognition failed",
            f"Expected {_PROBE_IMAGE_WORD!r}; received {brief(reply, 60)!r} ({elapsed_ms} ms).",
            "Check that the selected model supports image input.",
        )
    u = asst.usage
    usage_str = (
        f"{u.prompt_tokens} input + {u.completion_tokens} output tokens"
        if u.prompt_tokens
        else "Token usage unavailable"
    )
    return _fmt_ok(
        "API test",
        f"Image recognized · {elapsed_ms} ms",
        f"Reply: {brief(reply, 40)!r} · {usage_str}",
    )


def doctor(
    deep: Annotated[
        bool,
        typer.Option(
            "--deep",
            help="Also load the vision model, check camera frames, and test the model API. "
            "Sends one API request that may incur a small charge. May take several seconds.",
        ),
    ] = False,
) -> None:
    """Check your setup and show what needs attention."""
    paths.ensure_dirs()
    report = DoctorReport()
    report.header(__version__, deep=deep)

    report.section("Environment")
    system, release = platform.system(), platform.release()
    if system == "Darwin" and (mac_version := platform.mac_ver()[0]):
        system, release = "macOS", mac_version
    report.check(_fmt_info("System", f"{system} {release} · {platform.machine()}"))
    report.check(_fmt_info("Python", sys.version.split()[0], sys.executable))
    uv = shutil.which("uv")
    report.check(_fmt_info("uv", uv or "Not installed (optional)"))
    report.check(_fmt_info("Data directory", str(paths.HOME)))

    report.section("Local setup")
    # Re-read the file: it may have changed since the process started.
    from physiclaw.common import config as _cfg

    cp = _cfg.config_path()
    if cp.exists():
        try:
            _cfg.load()
            report.check(_fmt_ok("Config", "Valid", str(cp)))
        except _cfg.ConfigError as e:
            report.check(
                _fmt_error(
                    "Config",
                    "Invalid config.toml",
                    str(e),
                    action=("physiclaw config edit", "Correct the configuration file."),
                )
            )
    else:
        report.check(
            _fmt_info("Config", "Using built-in defaults", f"No config file at {cp}")
        )

    model = paths.omniparser_onnx()
    if model.exists():
        size_mb = model.stat().st_size / 1024 / 1024
        report.check(
            _fmt_ok("Vision model", f"Installed · {size_mb:.1f} MB", str(model))
        )
        if deep:
            report.note("Loading the vision model…")
            report.check(_probe_vision_model_deep())
    else:
        report.check(
            _fmt_warn(
                "Vision model",
                "Not installed",
                str(model),
                action=(
                    "physiclaw setup local-vision-model",
                    "Download the local vision model.",
                ),
            )
        )

    bundle = paths.calibration_bundle()
    calibration_problem = not bundle.exists()
    if not calibration_problem:
        report.check(_fmt_ok("Calibration file", "Saved", str(bundle)))
        if deep:
            calibration_check = _probe_calibration_deep()
            calibration_problem = calibration_check.status in {"WARN", "ERROR"}
            report.check(calibration_check)
    else:
        report.check(_fmt_warn("Calibration file", "Not found", str(bundle)))

    report.section("Hardware")
    # Read once; the server's device state and model selection take precedence.
    live = runtime_state.read_live()
    host, port, bind_all, status = _probe_server(live)
    status_problem = None
    if status is None and live:
        status_problem = "Process running, but status unavailable"
    elif status is not None and not isinstance(status, dict):
        status_problem = "Invalid status response"
    if status_problem:
        report.check(
            _fmt_error(
                "Server",
                status_problem,
                "Local hardware checks skipped because the server may be using the devices.",
                action=(
                    None,
                    f"Review {paths.LOG_DIR / 'server.log'} and the server's /api/status response.",
                ),
            )
        )
    elif isinstance(status, dict):
        from physiclaw.common.config import url_host

        report.check(_fmt_ok("Server", "Running", f"http://{url_host(host)}:{port}"))
        report.note("Device status reported by the running server.")
        if bind_all:
            report.check(
                _fmt_warn(
                    "Network access",
                    "Server control is exposed to the local network",
                    "A wildcard address (0.0.0.0 or ::) allows other devices to control the arm.",
                    "The phone bridge uses a separate port; it does not need this setting.",
                    action=(
                        "physiclaw config edit",
                        'Use server.host = "127.0.0.1" unless remote control is intended.',
                    ),
                )
            )
        for key, label, yes, no in (
            ("arm", "Arm", "Connected", "Not connected"),
            ("camera", "Camera", "Connected", "Not connected"),
            ("calibrated", "Calibration", "Complete", "Incomplete"),
            ("ready", "Hardware setup", "Ready", "Not ready; finish hardware setup"),
        ):
            passed = status.get(key) is True
            result = _fmt_ok if passed else _fmt_warn
            report.check(result(label, yes if passed else no))
        if deep:
            report.check(_probe_bridge_deep(host, port))
    else:
        for check in _offline_hardware_lines(deep):
            report.check(check)

    if not status_problem:
        if status is None:
            report.add_action(
                "physiclaw", "Start the server and open the hardware setup wizard."
            )
        elif not ready_from_status(status) or calibration_problem:
            report.add_action(
                "physiclaw setup hardware", "Complete the hardware setup wizard."
            )

    report.section("AI model")
    if deep:
        report.note("The API test sends an image to the selected model.")
    for check in _engine_lines(live, deep=deep):
        report.check(check)

    report.section("Skills")
    for check in _skills_lines():
        report.check(check)

    report.finish(deep=deep)
    maybe_print_update_banner()
