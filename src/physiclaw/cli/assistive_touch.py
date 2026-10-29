"""Run a standalone AssistiveTouch test on the usual PhysiClaw ports."""

import signal
import sys
import threading
import webbrowser
from collections.abc import Callable
from contextlib import ExitStack
from typing import Annotated

import typer

from physiclaw.cli._format import exit_error, info, section
from physiclaw.cli._service_handoff import ASSISTIVE_TOUCH_SERVICE, stop_service
from physiclaw.common import config, paths, runtime_state
from physiclaw.common.terminal import color_enabled, stdout_text
from physiclaw.port.locks import FileLockBusy, exclusive_file_lock


def _serve(servers, on_started: Callable[[], None]) -> None:
    """Own both HTTP loops; closing the command closes both listeners."""
    with ExitStack() as cleanup:
        if threading.current_thread() is threading.main_thread():
            previous = signal.signal(signal.SIGTERM, signal.default_int_handler)
            cleanup.callback(signal.signal, signal.SIGTERM, previous)
        threads = []
        for server in servers:
            thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.1},
                daemon=True,
            )
            thread.start()
            threads.append(thread)
            cleanup.callback(thread.join)
            cleanup.callback(server.shutdown)
        on_started()
        idle = threading.Event()
        while all(thread.is_alive() for thread in threads):
            idle.wait(0.2)
        exit_error("The AssistiveTouch test stopped unexpectedly.")


def _open_page(url: str, connection: dict, open_browser: bool) -> None:
    hostname = connection.get("hostname") or ""
    host = hostname or connection.get("ip")
    base = f"http://{host}:{connection['port']}/api/bridge" if host else None
    use_color = color_enabled(sys.stdout)
    typer.echo(section("Test AssistiveTouch"))
    for label, value in (
        ("Upload screenshot", f"{base}/screenshot" if base else "Unavailable"),
        ("Copy to clipboard", f"{base}/clipboard" if base else "Unavailable"),
        ("Host name", hostname.removesuffix(".local") or "Unavailable"),
        ("LAN IP", connection.get("ip") or "Unavailable"),
        ("Port", connection["port"]),
    ):
        label = f"{label:<19}"
        text = stdout_text(str(value))
        if use_color:
            label = typer.style(label, bold=True)
            if value == "Unavailable":
                text = typer.style(text, dim=True)
            else:
                text = typer.style(text, fg=typer.colors.CYAN)
        typer.echo(f"  {label}{text}")
    if open_browser:
        try:
            opened = webbrowser.open(url)
        except (OSError, webbrowser.Error):
            opened = False
        if not opened:
            typer.echo(info(f"Open in your browser: {url}"))


def test_assistive_touch(
    port: Annotated[
        int | None,
        typer.Option(
            "--port",
            min=1,
            max=65534,
            help="Control port; the phone bridge uses the next port. Default: live server or config.",
        ),
    ] = None,
    open_browser: Annotated[
        bool, typer.Option("--open/--no-open", help="Open the browser automatically.")
    ] = True,
) -> None:
    """Open a page to test AssistiveTouch screenshot uploads and clipboard transfer.

    Runs a small Python HTTP server on the usual ports. A running PhysiClaw
    server stops gracefully first; starting PhysiClaw again stops this test.
    On Windows, stop the current command with Ctrl-C before switching.
    No hardware or agent is needed. Ctrl-C ends the test.
    """
    live = runtime_state.read_live()
    selected_port = (
        port
        if port is not None
        else (live["port"] if live else config.CONFIG.server.port)
    )
    if not 1 <= selected_port <= 65534:
        exit_error(
            "Choose a control port from 1 to 65534; the phone bridge needs the next port."
        )
    if live:
        stop_service(live)

    from physiclaw.cli._assistive_touch_http import (
        PAGE_PATH,
        TestSession,
        connection_info,
        open_server,
    )

    try:
        with ExitStack() as resources:
            resources.enter_context(
                exclusive_file_lock(paths.runtime_state_file().with_suffix(".lock"))
            )
            state = TestSession()
            # Reserve both ports before publishing state or opening the page.
            desktop = resources.enter_context(
                open_server("127.0.0.1", selected_port, state, desktop=True)
            )
            phone = resources.enter_context(
                open_server("0.0.0.0", selected_port + 1, state, desktop=False)
            )
            connection = connection_info(selected_port + 1)
            state.connection = connection
            resources.callback(runtime_state.clear)
            url = f"http://127.0.0.1:{selected_port}{PAGE_PATH}"

            def started() -> None:
                runtime_state.write(
                    "127.0.0.1",
                    selected_port,
                    no_runtime=True,
                    service=ASSISTIVE_TOUCH_SERVICE,
                )
                _open_page(url, connection, open_browser)

            _serve((desktop, phone), started)
    except KeyboardInterrupt:
        pass
    except FileLockBusy:
        exit_error("Another PhysiClaw command is starting. Wait, then try again.")
    except OSError as exc:
        exit_error(f"Could not start the AssistiveTouch test: {exc}")
