"""Graceful handoff between the main server and the AssistiveTouch test."""

import time

import typer

from physiclaw.cli._format import exit_error
from physiclaw.port import processes

ASSISTIVE_TOUCH_SERVICE = "test-assistive-touch"
STOP_TIMEOUT_SECONDS = 30.0


def stop_service(live: dict) -> None:
    """Wait for the recorded process to release its resources; never force-kill."""
    pid = live["pid"]
    label = (
        "AssistiveTouch test"
        if live.get("service") == ASSISTIVE_TOUCH_SERVICE
        else "PhysiClaw server"
    )
    if not processes.pid_alive(pid):
        return
    if not processes.remote_shutdown_supported():
        exit_error(f"Stop the {label} with Ctrl-C in its terminal, then try again.")
    typer.echo(f"Stopping {label} (pid {pid})…")
    try:
        processes.request_shutdown(pid)
    except ProcessLookupError:
        return
    except OSError as exc:
        exit_error(f"Could not stop the {label}: {exc}")
    deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
    while processes.pid_alive(pid):
        if time.monotonic() >= deadline:
            exit_error(f"The {label} is still shutting down. Wait, then try again.")
        time.sleep(0.1)
