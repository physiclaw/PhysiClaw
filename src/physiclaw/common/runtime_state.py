"""Live-server state file: ``~/.physiclaw/run/server.json``.

Written by ``physiclaw server`` when it binds, removed on shutdown.
``physiclaw doctor`` reads this to learn the *actual* host/port the server
is on — which can differ from ``config.toml`` when ``--port`` was passed
at startup.

Stale files (server crashed without cleanup) are detected via a pid liveness
check in :func:`read_live`.
"""

import json
import logging
import os
import time

from physiclaw.common import paths
from physiclaw.common.text import read_text, write_text
from physiclaw.port import processes

log = logging.getLogger(__name__)


def write(
    host: str,
    port: int,
    *,
    model_ref: str | None = None,
    model_source: str | None = None,
    model_error: str | None = None,
    no_runtime: bool = False,
    service: str | None = None,
) -> None:
    """Record this process as the running server. Overwrites any prior file.

    ``model_ref`` / ``model_source`` capture the `provider/model` ref the
    server resolved at startup so ``physiclaw doctor`` in a different
    shell can report the live choice instead of re-running ``resolve()``
    against a shell that doesn't have the env var set. ``model_error``
    preserves a failed startup selection for the same reason. ``no_runtime``
    identifies an external MCP session, which needs no provider credentials.
    ``service`` identifies a standalone CLI listener for graceful handoff.
    """
    p = paths.runtime_state_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    write_text(
        p,
        json.dumps(
            {
                "pid": os.getpid(),
                "host": host,
                "port": port,
                "model_ref": model_ref,
                "model_source": model_source,
                "model_error": model_error,
                "no_runtime": no_runtime,
                "started_at": time.time(),
                **({"service": service} if service else {}),
            }
        ),
    )


def clear() -> None:
    """Best-effort delete of this process's state file. Refuses to remove
    a file written by a different pid so stray debug scripts can't kill
    a live server's recorded state (the server re-writes only on its
    own startup, not per-request, so a mis-clear would persist until
    restart).
    """
    p = paths.runtime_state_file()
    try:
        state = json.loads(read_text(p))
    except (OSError, ValueError):  # ValueError covers JSON + UTF-8 decode errors
        return
    if not isinstance(state, dict) or state.get("pid") != os.getpid():
        return
    try:
        p.unlink()
    except OSError:
        pass


def read_live() -> dict | None:
    """Return the recorded server state if its process is still alive.

    Returns ``None`` for missing/malformed state or a stale pid.
    Doesn't delete stale files — the next ``server`` start overwrites them.
    """
    p = paths.runtime_state_file()
    try:
        state = json.loads(read_text(p))
    except (OSError, ValueError):  # ValueError covers JSON + UTF-8 decode errors
        return None
    if not isinstance(state, dict):
        return None
    pid, host, port = state.get("pid"), state.get("host"), state.get("port")
    # Zero/negative pids signal process groups on POSIX. Windows accepts
    # a DWORD, so larger integers could wrap to an unrelated process.
    if type(pid) is not int or not 0 < pid <= 0xFFFFFFFF:
        return None
    if not isinstance(host, str) or not host or type(port) is not int:
        return None
    if not 1 <= port <= 65535 or not _pid_alive(pid):
        return None
    return state


def request_shutdown() -> bool:
    """Ask the recorded live server to shut down gracefully; True when
    the signal was sent. Owns the platform signal choice the way
    `_pid_alive` owns the liveness probe: SIGINT takes uvicorn's
    graceful path (arm parks, atexit reaps the runtime) on POSIX, and on
    Windows — where os.kill with SIGINT would TerminateProcess — the
    caller just gets False and a log line instead of a hard kill."""
    state = read_live()
    if state is None:
        log.warning("no live server recorded — nothing to shut down")
        return False
    if not processes.remote_shutdown_supported():
        log.warning(
            "graceful remote shutdown unsupported on this platform — stop it yourself"
        )
        return False
    try:
        processes.request_shutdown(int(state["pid"]))
        return True
    except Exception:
        log.warning("could not signal server pid %s", state.get("pid"), exc_info=True)
        return False


def _pid_alive(pid: int) -> bool:
    """True if a process with this pid currently exists."""
    return processes.pid_alive(pid)
