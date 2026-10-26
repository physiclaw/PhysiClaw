"""Application process operations; callers own runtime policy and shutdown budgets."""

import subprocess

from physiclaw.port._select import native as _native


def spawn_managed(argv: list[str]) -> subprocess.Popen:
    """Isolate terminal signals; the caller requests graceful shutdown via stdin EOF."""
    return subprocess.Popen(
        argv, stdin=subprocess.PIPE, **_native.process_group_options()
    )


def prepare_managed_runtime() -> None:
    """Perform native initialization before a managed control reader starts."""
    _native.prepare_managed_runtime()


def pid_alive(pid: int) -> bool:
    return _native.pid_alive(pid)


def remote_shutdown_supported() -> bool:
    return _native.REMOTE_SHUTDOWN_SUPPORTED


def request_shutdown(pid: int) -> None:
    _native.request_shutdown(pid)
