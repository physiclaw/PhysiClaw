"""``physiclaw server`` — run the MCP server (and the agent runtime subprocess)."""

import atexit
import logging
import os
import signal
import subprocess
import sys
import threading
from typing import Annotated, Optional

import typer


def _spawn_runtime(port: int, verbose: bool) -> subprocess.Popen:
    """Run the hook loop out-of-process so long-running hooks don't block
    the MCP event loop. Terminated via atexit when the server exits.

    Engine + provider are picked by PHYSICLAW_PROVIDER (or auto-detected
    from API-key env vars). The subprocess inherits the env.
    """
    from physiclaw.agent.runtime.config import (
        EXTERNAL,
        PROVIDER_DEFAULT,
        PROVIDER_ENV_VAR,
    )

    log = logging.getLogger(__name__)
    cmd = [
        sys.executable,
        "-m",
        "physiclaw.agent.runtime",
        "--server",
        f"http://127.0.0.1:{port}",
    ]
    if verbose:
        cmd.append("--verbose")
    choice = os.environ.get(PROVIDER_ENV_VAR, PROVIDER_DEFAULT)
    label = (
        "engine=claude-code"
        if choice == EXTERNAL
        else f"engine=physiclaw, provider={choice}"
    )
    proc = subprocess.Popen(cmd)
    log.info(f"Runtime loop started as subprocess (pid={proc.pid}, {label})")
    return proc


def server(
    port: Annotated[int, typer.Option("--port", help="MCP server port.")] = 8048,
    host: Annotated[str, typer.Option("--host", help="Bind address.")] = "0.0.0.0",
    verbose: Annotated[
        bool,
        typer.Option("-v", "--verbose", help="Show detailed debug output."),
    ] = False,
    no_runtime: Annotated[
        bool,
        typer.Option(
            "--no-runtime", help="Don't spawn the agent runtime loop subprocess."
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
    cam_index: Annotated[
        Optional[int],
        typer.Option(
            "--cam-index",
            help="Camera index override for --warm-start (default: value "
            "stored in the bundle, falling back to 0).",
        ),
    ] = None,
    save_tool_calls: Annotated[
        bool,
        typer.Option(
            "--save-tool-calls",
            help="Write every peek/screenshot output under the user data dir.",
        ),
    ] = False,
    save_snapshots: Annotated[
        bool,
        typer.Option(
            "--save-snapshots",
            help="Write every raw camera frame under the user data dir.",
        ),
    ] = False,
    save_screenshots: Annotated[
        bool,
        typer.Option(
            "--save-screenshots",
            help="Write every raw phone-own screenshot under the user data dir.",
        ),
    ] = False,
) -> None:
    """Run the PhysiClaw MCP server."""
    from physiclaw.core.logger import setup_logging

    for enabled, env in (
        (save_tool_calls, "PHYSICLAW_SAVE_TOOL_CALLS"),
        (save_snapshots, "PHYSICLAW_SAVE_SNAPSHOTS"),
        (save_screenshots, "PHYSICLAW_SAVE_SCREENSHOTS"),
    ):
        if enabled:
            os.environ[env] = "1"

    setup_logging("physiclaw", logging.DEBUG if verbose else logging.INFO)
    logging.getLogger("mcp").setLevel(logging.WARNING)
    from physiclaw.core.server import mcp, shutdown

    atexit.register(shutdown)

    mcp.settings.host = host
    mcp.settings.port = port
    mcp.settings.log_level = "WARNING"

    from physiclaw.core.bridge import bridge_base_urls

    log = logging.getLogger(__name__)
    primary, fallback = bridge_base_urls(port)
    display_host = "localhost" if host == "0.0.0.0" else host
    log.info(f"PhysiClaw MCP server on http://{display_host}:{port}/mcp")
    log.info(f"QR code (scan with phone): http://localhost:{port}/api/bridge/qr")
    if primary != fallback:
        log.info(
            f"Phone page: {primary}/bridge  (recommended — survives IP changes)"
        )
        log.info(f"Fallback:   {fallback}/bridge  (if mDNS blocked)")
    else:
        log.info(f"Phone page: {fallback}/bridge")
        log.info(
            "Tip: set a stable LocalHostName for <name>.local URLs — "
            "see `physiclaw setup phone` (coming soon)."
        )
    if warm_start:
        # Run warm-start in a background thread so mcp.run() below can start
        # serving HTTP first — the phone needs the server listening to load
        # /bridge and POST screen_dimension / touches. On failure, send
        # SIGINT to the main thread so mcp.run exits and atexit handlers
        # (shutdown, arm return-to-origin) still fire cleanly.
        from physiclaw.core.server import warm_start as ws

        def _warm_start_thread() -> None:
            if not ws.wait_for_port(host, port):
                log.error(
                    "warm-start: server never started accepting connections; "
                    "exiting."
                )
                os.kill(os.getpid(), signal.SIGINT)
                return
            if not ws.try_resume(cam_index):
                log.error(
                    "Exiting. Re-run without --warm-start, then "
                    "`physiclaw setup hardware` to recalibrate."
                )
                os.kill(os.getpid(), signal.SIGINT)

        threading.Thread(target=_warm_start_thread, daemon=True).start()
    else:
        log.info(
            "Run `physiclaw setup hardware` in another shell to connect "
            "hardware and calibrate — server is waiting."
        )

    if not no_runtime:
        runtime_proc = _spawn_runtime(port, verbose)

        def _stop_runtime() -> None:
            if runtime_proc.poll() is None:
                runtime_proc.terminate()
                try:
                    runtime_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    runtime_proc.kill()

        atexit.register(_stop_runtime)

    try:
        mcp.run(transport="streamable-http")
    except KeyboardInterrupt:
        pass
