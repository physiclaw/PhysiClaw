"""``physiclaw server`` — run the MCP server (and the agent runtime subprocess)."""

import _thread
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
from collections.abc import Callable
from contextlib import ExitStack
from functools import partial
from typing import Annotated, Optional

import typer

from physiclaw.common.config import CONFIG, WILDCARD_HOSTS, url_host


def server(
    port: Annotated[
        int,
        typer.Option(
            "--port",
            min=1,
            max=65534,
            help="Control-plane port (MCP + setup). The "
            "LAN phone bridge always listens on port+1.",
        ),
    ] = CONFIG.server.port,
    host: Annotated[
        str,
        typer.Option(
            "--host",
            help="Control-plane bind address. Loopback "
            "by default — the phone bridge is the only plane that binds 0.0.0.0.",
        ),
    ] = CONFIG.server.host,
    verbose: Annotated[
        bool,
        typer.Option("-v", "--verbose", help="Show detailed debug output."),
    ] = False,
    no_runtime: Annotated[
        bool,
        typer.Option(
            "--no-runtime",
            help="Don't spawn the agent runtime loop subprocess "
            "(the `physiclaw mcp` mode).",
        ),
    ] = False,
    warm_start: Annotated[
        bool,
        typer.Option(
            "--warm-start",
            help="Auto-connect hardware from the saved calibration bundle and "
            "mark ready, skipping `setup hardware`. Falls through if the "
            "bundle is incomplete or hardware connect fails.",
        ),
    ] = False,
    hot_start: Annotated[
        bool,
        typer.Option(
            "-H",
            "--hot-start",
            help="Like --warm-start, but skip every check that touches the "
            "phone (bridge wait, sanity tap): trust that the arm still sits "
            "where it parked and mark ready as soon as hardware reconnects. "
            "Use only when nothing moved since the last run. "
            "Shortcut: `physiclaw now`.",
        ),
    ] = False,
    cam_index: Annotated[
        Optional[int],
        typer.Option(
            "--cam-index",
            help="Camera index override for --warm-start / --hot-start "
            "(default: value stored in the bundle, falling back to 0).",
        ),
    ] = None,
    no_setup_hardware: Annotated[
        bool,
        typer.Option(
            "--no-setup-hardware",
            help="Don't auto-open the browser hardware-setup wizard on start.",
        ),
    ] = False,
    auto_calibrate: Annotated[
        bool,
        typer.Option(
            "--auto-calibrate",
            hidden=True,
            help="Internal (used by `physiclaw auto`): skip the desktop wizard "
            "and calibrate unattended when the phone bridge opens.",
        ),
    ] = False,
    save_tool_calls: Annotated[
        bool,
        typer.Option(
            "--save-tool-calls",
            help="Write every peek/screenshot output under the user data dir.",
        ),
    ] = CONFIG.server.save_tool_calls,
    save_snapshots: Annotated[
        bool,
        typer.Option(
            "--save-snapshots",
            help="Write each snapshot frame (rotated, with the bbox overlay) "
            "under the user data dir.",
        ),
    ] = CONFIG.server.save_snapshots,
    save_screenshots: Annotated[
        bool,
        typer.Option(
            "--save-screenshots",
            help="Write every raw phone-own screenshot under the user data dir.",
        ),
    ] = CONFIG.server.save_screenshots,
    save_raw_camera: Annotated[
        bool,
        typer.Option(
            "--save-raw-camera",
            help="Write every raw camera frame as it's captured under the user "
            "data dir (calibration, peek, snapshot). Clear with "
            "`physiclaw clear`.",
        ),
    ] = CONFIG.server.save_raw_camera,
) -> None:
    """Run the PhysiClaw MCP server.

    Reserve both listeners before recording state or starting hardware and the
    runtime. One scope owns the server lock, listeners, state and child process;
    returning or failing releases them in reverse order.
    """
    from physiclaw import __version__
    from physiclaw.cli._format import exit_error
    from physiclaw.common import paths, runtime_state
    from physiclaw.common.file_lock import FileLockBusy, exclusive_file_lock
    from physiclaw.common.logger import (
        SERVER_LOG_TAG,
        attach_server_mcp_tee,
        setup_logging,
    )
    from physiclaw.core.bridge.lan import bridge_port

    # Direct callers (mcp/now/auto) bypass Typer's option validation.
    if not 1 <= port <= 65534:
        exit_error("--port must be between 1 and 65534 (bridge uses port + 1)", code=2)

    # Logging first: everything from here on — including the version line and
    # the background auto-sync's outcome — speaks in one voice, the
    # timestamped `[physiclaw]` stream. The daily file is DEBUG regardless of
    # console verbosity (the server's own persistent log).
    setup_logging(
        SERVER_LOG_TAG,
        logging.DEBUG if verbose else logging.INFO,
        file_dir=paths.server_log_dir(),
        file_level=logging.DEBUG,
    )
    # Tee this process's camera/exposure/tune detail into the agent's active
    # session dir as mcp.log (the runtime publishes which session is active).
    attach_server_mcp_tee()
    logging.getLogger("mcp").setLevel(logging.WARNING)
    # Phones opening the bridge URL probe https:// first (Safari's HTTPS
    # upgrade, VPN apps); the TLS bytes hitting our plaintext port make
    # uvicorn warn "Invalid HTTP request received." once per attempt —
    # noise with no action for the user. Drop that one message; every
    # other uvicorn warning still surfaces.
    logging.getLogger("uvicorn.error").addFilter(
        lambda record: "Invalid HTTP request received" not in record.getMessage()
    )
    log = logging.getLogger(__name__)
    log.info(
        "PhysiClaw %s",
        __version__,
        extra={
            "console": True,
            "console_message": f"PhysiClaw {__version__} — starting hardware",
        },
    )
    _require_vision_model()
    with ExitStack() as resources:
        # Uvicorn replays SIGTERM after graceful shutdown. Translate that replay
        # into KeyboardInterrupt so Python cleanup runs before restoring it.
        if threading.current_thread() is threading.main_thread():
            previous = signal.signal(signal.SIGTERM, signal.default_int_handler)
            resources.callback(signal.signal, signal.SIGTERM, previous)
        try:
            resources.enter_context(
                exclusive_file_lock(paths.runtime_state_file().with_suffix(".lock"))
            )
        except FileLockBusy:
            exit_error("another PhysiClaw server is running or starting")
        except OSError as e:
            exit_error(f"cannot acquire the server lock: {e}")
        _refuse_if_already_running(host, port)
        listeners = (
            resources.enter_context(_open_listener(host, port)),
            resources.enter_context(_open_listener("0.0.0.0", bridge_port(port))),
        )
        _run_startup_maintenance(sync_skills=not no_runtime)
        _apply_save_flags(
            save_tool_calls, save_snapshots, save_screenshots, save_raw_camera
        )

        from physiclaw.core.server import build_apps, shutdown

        resources.callback(shutdown)
        resources.callback(runtime_state.clear)
        model_ref, runtime_label = _resolve_and_record_model(
            host, port, no_runtime=no_runtime
        )
        _log_endpoints(host, port, no_runtime=no_runtime)
        apps = build_apps(host)
        child = _start_runtime_loop(
            host,
            port,
            verbose,
            no_runtime=no_runtime,
            model_ref=model_ref,
            runtime_label=runtime_label,
        )
        if child is not None:
            resources.callback(terminate_child, child)
        try:
            _serve(
                host,
                port,
                *apps,
                sockets=listeners,
                announce_ready=no_runtime,
                on_started=partial(
                    _start_hardware_bringup,
                    host,
                    port,
                    warm_start=warm_start,
                    hot_start=hot_start,
                    cam_index=cam_index,
                    auto_calibrate=auto_calibrate,
                    no_setup_hardware=no_setup_hardware,
                ),
            )
        except KeyboardInterrupt:
            pass


def _open_listener(host: str, port: int) -> socket.socket:
    """Reserve an address before startup side effects; the caller owns closing."""
    from physiclaw.cli._format import exit_error

    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        return socket.create_server(
            (host, port),
            family=family,
            dualstack_ipv6=family == socket.AF_INET6 and socket.has_dualstack_ipv6(),
        )
    except OSError as e:
        exit_error(f"cannot listen on {host}:{port}: {e}")


def _serve(
    host: str,
    port: int,
    control_app,
    bridge_app,
    *,
    sockets: tuple[socket.socket, socket.socket],
    announce_ready: bool,
    on_started: Callable[[], None],
) -> None:
    """Serve the two planes from one process, two listeners.

    Control (MCP + setup + calibrate) binds `host` (loopback by default) on
    `port`; the phone bridge binds 0.0.0.0 on `port`+1 so ONLY those seven
    routes are reachable from the LAN — the arm-driving surface can't be.
    Two listeners because a wildcard and a specific bind on one port don't
    coexist on Linux.

    The bridge runs in a daemon thread (uvicorn skips signal handling off
    the main thread); the control server keeps the main thread, so Ctrl-C
    uvicorn handles signals and shuts down gracefully. A failed bridge stops
    the control listener too; leaving this scope cancels startup and joins both
    worker threads. Hardware bring-up starts only after both ASGI apps are up.
    """
    import uvicorn

    from physiclaw.cli._format import exit_error
    from physiclaw.core.bridge.lan import bridge_port

    stopping = threading.Event()
    bridge_server = uvicorn.Server(
        uvicorn.Config(
            bridge_app,
            host="0.0.0.0",
            port=bridge_port(port),
            log_level="warning",
            log_config=None,
            access_log=False,
        )
    )
    control_server = uvicorn.Server(
        uvicorn.Config(
            control_app,
            host=host,
            port=port,
            log_level="warning",
            log_config=None,
            access_log=False,
        )
    )
    failures: list[str] = []

    def run_bridge() -> None:
        try:
            bridge_server.run(sockets=[sockets[1]])
        except BaseException as e:
            failures.append(f"phone bridge failed: {str(e) or type(e).__name__}")
        finally:
            if not stopping.is_set():
                if not failures:
                    failures.append(
                        "phone bridge failed: listener stopped unexpectedly"
                    )
                control_server.should_exit = True

    def after_startup() -> None:
        # Reserved sockets already accept TCP connections. Hardware must wait
        # for both ASGI lifespans to complete, not merely a successful connect.
        while not stopping.wait(0.01):
            if control_server.should_exit or bridge_server.should_exit:
                return
            if control_server.started and bridge_server.started:
                try:
                    on_started()
                    if announce_ready:
                        _announce_ready_when_up(host, port, stopping=stopping)
                except BaseException as e:
                    failures.append(
                        f"hardware startup failed: {str(e) or type(e).__name__}"
                    )
                    control_server.should_exit = True
                return

    threads = (
        threading.Thread(target=run_bridge, name="bridge-http", daemon=True),
        threading.Thread(target=after_startup, name="server-startup", daemon=True),
    )
    interrupted = False
    try:
        for thread in threads:
            thread.start()
        control_server.run(sockets=[sockets[0]])
    except KeyboardInterrupt:
        interrupted = True
    finally:
        stopping.set()
        bridge_server.should_exit = True
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=5)
    if failures:
        exit_error(failures[0])
    for thread in threads:
        if thread.is_alive():
            exit_error(f"{thread.name} did not stop within 5 seconds")
    if interrupted:
        raise KeyboardInterrupt
    if not control_server.started:
        exit_error("control server failed to start")


def _announce_ready_when_up(
    host: str, port: int, *, stopping: threading.Event | None = None
) -> None:
    """`physiclaw mcp` mode: one ready line when the rig can actually be
    driven — /api/status `ready: true`, the same flag the built-in
    runtime polls before waking (`runtime._check_ready`).

    A port-accept probe is NOT that moment: serving starts while
    hardware bring-up (hot-start resume, or the setup wizard) is still
    running, and an MCP client connecting then would watch its tool
    calls fail. With a built-in runtime its subprocess already logs
    `physiclaw ready=True`, so this watcher runs only without one.
    Daemon thread, no deadline — the wizard flow flips ready whenever
    the user finishes calibrating; connection errors are pre-serving
    or mid-blip states, held and retried like the runtime does. The serving
    scope signals `stopping` to cancel the watcher on shutdown."""
    log = logging.getLogger(__name__)
    # The shared ready definition (`common.ready`); its httpx transport
    # loads on the first probe, not at import.
    from physiclaw.common.ready import check_ready_once

    base = f"http://{url_host(host)}:{port}"
    stopping = stopping or threading.Event()

    def _watch() -> None:
        while not stopping.is_set():
            try:
                if check_ready_once(base):
                    if not stopping.is_set():
                        log.info(
                            "PhysiClaw ready — MCP tools live at %s/mcp",
                            base,
                            extra={"console": True},
                        )
                    return
            except Exception:
                pass
            stopping.wait(1.0)

    threading.Thread(target=_watch, daemon=True).start()


def _require_vision_model() -> None:
    """Exit at startup if the OmniParser icon-detection model isn't installed.

    `screenshot()` — which every screen-reading wake relies on — raises without
    it (see ``core.vision.icon_detect``). It's a hard requirement for the server
    to be useful, so fail fast here with the one-line fix rather than letting the
    runtime boot and die on its first screenshot mid-task."""
    from physiclaw.cli._format import exit_error
    from physiclaw.common import paths

    model = paths.omniparser_onnx()
    if not model.exists():
        exit_error(
            f"vision model not installed — {model} is missing.\n"
            "Run: physiclaw setup local-vision-model"
        )


def _run_startup_maintenance(*, sync_skills: bool) -> None:
    """Startup housekeeping that must NOT block serving.

    Phase A update check: if a prior run staged a newer version, print a
    "run `uv tool upgrade physiclaw`" notice and keep going. The server NEVER
    self-installs (reinstalling the venv under a live physiclaw corrupts it on
    Windows — see cli/update.py); the user upgrades manually via uv.

    Then two off-critical-path background flows:
      - skills: sync the official pack; a session picks it up at its next wake.
        Skipped when `sync_skills` is False (--no-runtime / `physiclaw mcp`):
        the pack feeds the in-tree engine's prompt, so with no runtime there
        is no consumer. The next runtime-full start re-syncs.
      - update: Phase B — stage the NEXT release (probe + warm uv's cache +
        marker) so the next start can notify it's ready and the user's
        `uv tool upgrade` links it from cache fast.
    """
    from physiclaw.cli.sync_official_skills import maybe_auto_sync
    from physiclaw.cli.update import notify_staged_update, start_stage_update_thread

    notify_staged_update()
    if sync_skills:
        maybe_auto_sync()
    start_stage_update_thread()


def _apply_save_flags(
    save_tool_calls: bool,
    save_snapshots: bool,
    save_screenshots: bool,
    save_raw_camera: bool,
) -> None:
    """Translate the --save-* flags into the env vars the core reads."""
    for enabled, env in (
        (save_tool_calls, "PHYSICLAW_SAVE_TOOL_CALLS"),
        (save_snapshots, "PHYSICLAW_SAVE_SNAPSHOTS"),
        (save_screenshots, "PHYSICLAW_SAVE_SCREENSHOTS"),
        (save_raw_camera, "PHYSICLAW_SAVE_RAW_CAMERA"),
    ):
        if enabled:
            os.environ[env] = "1"


def _dial_host(host: str) -> str:
    """Bind address → an address a client can dial. A wildcard bind
    (0.0.0.0/::) listens on loopback too but can't itself be dialed.
    Socket-scoped (unbracketed IPv6) — URL builders use
    `config.url_host`, which additionally brackets IPv6 literals."""
    return "127.0.0.1" if host in WILDCARD_HOSTS else host


def _port_in_use(host: str, port: int) -> bool:
    """Quick connect probe of a control or bridge port."""
    try:
        with socket.create_connection((_dial_host(host), port), timeout=0.5):
            return True
    except OSError:
        return False


def _refuse_if_already_running(host: str, port: int) -> None:
    """Exit before touching runtime state when a server is already live.

    Without this, a second `physiclaw server` writes runtime_state (clobbering
    the live server's record), fails the port bind, and its cleanup
    then erases the record entirely — doctor/setup lose the LIVE server.
    read_live() is pid-checked, so a stale file from a crash never blocks a
    start; the socket probe catches a live listener whose state file is gone.
    """
    from physiclaw.cli._format import exit_error
    from physiclaw.common import runtime_state
    from physiclaw.core.bridge.lan import bridge_port

    live = runtime_state.read_live()
    if live:
        exit_error(
            f"physiclaw server already running (pid {live.get('pid')}, "
            f"http://{live.get('host')}:{live.get('port')}). "
            "Stop it first — the runtime-state record is single-slot, so "
            "only one physiclaw server can use this data directory."
        )
    # On macOS a wildcard SO_REUSEADDR listener can coexist with a more
    # specific listener, which would receive the loopback traffic instead.
    for bind, number in ((host, port), ("0.0.0.0", bridge_port(port))):
        if _port_in_use(bind, number):
            exit_error(
                f"port {number} is already accepting connections on "
                f"{_dial_host(bind)} — is another server running? "
                "Stop it first, or pass a different --port."
            )


def _resolve_and_record_model(
    host: str, port: int, *, no_runtime: bool = False
) -> tuple[Optional[str], str]:
    """Record the startup selection so doctor doesn't resolve another shell's model.

    External MCP mode needs no model. Invalid selections keep the HTTP server
    running, record the cause, and skip the runtime. The caller clears the
    state on shutdown; stale processes are handled by runtime_state.read_live.
    """
    from physiclaw.common import runtime_state

    if no_runtime:
        runtime_state.write(host, port, no_runtime=True)
        return None, "external MCP client"

    from physiclaw.agent.runtime.launcher import engine_label
    from physiclaw.agent.runtime.launcher import resolve as _resolve_model

    try:
        model_ref, model_source = _resolve_model()
        runtime_label = engine_label(model_ref)
    except RuntimeError as e:
        logging.getLogger(__name__).warning("Runtime loop NOT started — %s", e)
        runtime_state.write(host, port, model_error=str(e))
        return None, "engine=(unset)"

    runtime_state.write(host, port, model_ref=model_ref, model_source=model_source)
    return model_ref, runtime_label


def _log_endpoints(host: str, port: int, *, no_runtime: bool) -> None:
    """Log the MCP URL, the QR link, and the phone /bridge URLs — primary
    (mDNS, survives IP changes) plus IP fallback, or a single URL and a
    LocalHostName tip when the two coincide. Without a built-in runtime
    (--no-runtime / `physiclaw mcp`) an external MCP client is the consumer,
    so the /mcp line carries the connect hint — dialable via
    `config.url_host`, since the client pastes it."""
    log = logging.getLogger(__name__)
    from physiclaw.common import paths
    from physiclaw.core.bridge import bridge_base_urls

    primary, fallback = bridge_base_urls(port)
    base = f"http://{url_host(host)}:{port}"
    log.info("MCP: %s/mcp", base, extra={"console": no_runtime})
    log.info("Phone: %s/bridge", primary, extra={"console": True})
    log.info("QR code: %s/api/bridge/qr (scan with your phone)", base)
    if primary != fallback:
        log.info("Fallback: %s/bridge (if mDNS is blocked)", fallback)
    else:
        log.info("Tip: set a stable LocalHostName for <name>.local URLs.")
    if no_runtime:
        log.info(
            "External agent: connect your MCP client, e.g. Claude Code:\n"
            "  claude mcp add --transport http physiclaw %s/mcp",
            base,
        )
    log.info("Logs: %s", paths.LOG_DIR, extra={"console": True})


def _start_hardware_bringup(
    host: str,
    port: int,
    *,
    warm_start: bool,
    hot_start: bool,
    cam_index: Optional[int],
    auto_calibrate: bool,
    no_setup_hardware: bool,
) -> None:
    """Dispatch hardware bring-up to one of five mutually exclusive modes. Each
    (except --no-setup-hardware, which just waits) runs in a daemon thread so
    HTTP serving stays responsive during setup. The caller waits for both
    servers to start first. --hot-start skips warm-start verification; when
    both are passed the verifying mode wins."""
    if warm_start or hot_start:
        _warm_start_hardware(host, port, cam_index, verify=warm_start)
    elif auto_calibrate:
        _auto_calibrate_hardware(host, port)
    elif no_setup_hardware:
        logging.getLogger(__name__).info(
            "Run `physiclaw setup hardware` in another shell to connect "
            "hardware and calibrate — server is waiting.",
            extra={"console": True},
        )
    else:
        _open_hardware_wizard(host, port)


def _warm_start_hardware(
    host: str, port: int, cam_index: Optional[int], *, verify: bool
) -> None:
    """--warm-start / --hot-start: resume from the saved calibration bundle in
    a background thread (`verify=False` = --hot-start, which skips the bridge
    wait and sanity tap). On failure, raise KeyboardInterrupt in the main
    thread (via _thread.interrupt_main — cross-platform; os.kill(SIGINT) on
    Windows would TerminateProcess and skip cleanup) so serving stops and
    the owning scope shuts down hardware and parks the arm cleanly."""
    log = logging.getLogger(__name__)
    from physiclaw.core.server import warm_start as ws

    flag = ws.resume_flag(verify)

    def _warm_start_thread() -> None:
        from physiclaw.core.server.app import default_bundle

        if not ws.wait_for_port(host, port):
            log.error("%s: server never started accepting connections; exiting.", flag)
            _thread.interrupt_main()
            return
        try:
            resumed = ws.try_resume(
                cam_index,
                verify=verify,
                physiclaw=default_bundle.physiclaw,
                calib=default_bundle.calib,
                phone=default_bundle.phone,
            )
        except Exception:
            # A raise here would otherwise die with the daemon thread,
            # leaving the server up but never ready and no diagnosis.
            log.exception("%s: resume crashed", flag)
            resumed = False
        if not resumed:
            # Mode-neutral: the user may have typed `physiclaw now`,
            # `physiclaw server -H`, or `--warm-start` — try_resume already
            # named the failing mode AND the fix (plug in the board,
            # recalibrate, …) in its diagnosis, so prescribe nothing here.
            log.debug("Exiting. Fix the cause above and re-run.")
            _thread.interrupt_main()

    threading.Thread(target=_warm_start_thread, daemon=True).start()


def _auto_calibrate_hardware(host: str, port: int) -> None:
    """--auto-calibrate (`physiclaw auto`): no desktop wizard — a background
    thread waits for the phone bridge, then calibrates unattended. The worker
    lives in the setup module (with the wizard it drives); import it lazily so
    `physiclaw server` doesn't pull the CLI setup module in at startup."""
    log = logging.getLogger(__name__)
    log.info("Auto mode: calibration will start when the phone opens /bridge.")

    def _auto_calibrate_thread() -> None:
        import importlib

        hw = importlib.import_module("physiclaw.cli.setup.hardware")
        hw.await_bridge_and_calibrate(host, port)

    threading.Thread(target=_auto_calibrate_thread, daemon=True).start()


def _open_hardware_wizard(host: str, port: int) -> None:
    """Default mode: open the browser hardware-setup wizard once the server is
    actually accepting connections (the page immediately calls /api/status).
    Runs in a daemon thread to keep browser launch off the serving loop."""
    log = logging.getLogger(__name__)
    from physiclaw.core.server.warm_start import wait_for_port

    setup_url = f"http://{url_host(host)}:{port}/setup-hardware"
    log.info(
        f"Hardware-setup wizard: {setup_url}  (disable with --no-setup-hardware)",
        extra={"console": True, "console_message": f"Open hardware setup: {setup_url}"},
    )

    def _open_setup() -> None:
        import webbrowser

        if wait_for_port(host, port):
            try:
                webbrowser.open(setup_url)
            except Exception:  # noqa: BLE001 — never let a headless box crash startup
                log.debug("could not open browser for setup wizard", exc_info=True)

    threading.Thread(target=_open_setup, daemon=True).start()


def _start_runtime_loop(
    host: str,
    port: int,
    verbose: bool,
    *,
    no_runtime: bool,
    model_ref: Optional[str],
    runtime_label: str,
) -> subprocess.Popen | None:
    """Spawn the agent runtime subprocess for the caller to own,
    unless disabled (--no-runtime) or no model is configured."""
    log = logging.getLogger(__name__)
    if no_runtime:
        # The external-client connect hint rides the /mcp endpoint line
        # (`_log_endpoints`) — nothing to report here.
        return None
    if model_ref is None:
        # Resolution already reported the actual cause. Keep the hardware
        # server available for setup and external MCP clients.
        log.warning(
            "The MCP server is running for hardware setup and external clients. "
            "Configure an API model with `physiclaw models key <provider>` and "
            "`physiclaw models use <provider/model>`, then restart `physiclaw server`."
        )
        return None

    return _spawn_runtime(host, port, verbose, runtime_label)


def terminate_child(proc: subprocess.Popen, timeout: float = 5) -> None:
    """Stop and reap a child we spawned, killing it if termination times out."""
    if proc.poll() is None:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass  # it exited between poll and terminate; still reap it below
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            proc.wait()


def _spawn_runtime(host: str, port: int, verbose: bool, label: str) -> subprocess.Popen:
    """Run the hook loop out-of-process so long-running hooks don't block
    the MCP event loop. The caller terminates it when serving ends.

    `label` is the pre-resolved engine/provider/model string
    passed by the caller — the caller already did provider resolution to
    record into runtime_state, so reuse that instead of resolving again.
    """
    log = logging.getLogger(__name__)
    # Dial the configured bind, not hardcoded loopback — with --host set to
    # a LAN IP nothing listens on 127.0.0.1 (wildcard binds do, so map those).
    cmd = [
        sys.executable,
        "-m",
        "physiclaw.agent.runtime",
        "--server",
        f"http://{url_host(host)}:{port}",
    ]
    if verbose:
        cmd.append("--verbose")
    proc = subprocess.Popen(cmd)
    log.debug("Runtime loop started as subprocess (pid=%s, %s)", proc.pid, label)
    return proc
