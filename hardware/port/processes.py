"""Owned hardware child processes with bounded, platform-neutral cleanup.

The resource owner holds native tree state separately from Popen. Callers
cannot forget the native association or leave descendants behind on an error.
The cancellation marker is a scoped parent/child protocol needed by Windows
console launchers; no build decisions or CAD dependencies live here.
"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any, Protocol

from hardware.port import locks
from hardware.port._select import native as _native

_CANCEL_ENV = "PHYSICLAW_HW_CANCEL_FILE"


class _ProcessTree(Protocol):
    def stop(self, timeout: float, *, cooperative_cancel: bool = False) -> None:
        """Reap the leader and release every owned descendant resource."""


class CancellationEvent(Event):
    def is_set(self) -> bool:
        path = os.environ.get(_CANCEL_ENV)
        return super().is_set() or bool(path and Path(path).exists())


@dataclass
class OwnedProcess:
    """One child and its native tree; cleanup is idempotent."""

    process: subprocess.Popen
    _tree: _ProcessTree
    _stopped: bool = False

    def stop(self, timeout: float = 1.0, *, cooperative_cancel: bool = False) -> None:
        """Stop the tree; only marker-aware children may opt into cooperative exit."""
        if not self._stopped:
            self._tree.stop(timeout, cooperative_cancel=cooperative_cancel)
            self._stopped = True


@contextmanager
def owned_process(argv: Sequence[str], **kwargs: Any) -> Iterator[OwnedProcess]:
    """Launch a private child tree, inherit the output lease, and always reap.

    Workflow code may stop early to unblock readers; leaving the context also
    closes the owner on normal return, cancellation, or collection failure.
    """
    with ExitStack() as resources:
        with locks.child_inheritance() as lock_options:
            process = resources.enter_context(
                subprocess.Popen(
                    argv, **lock_options, **_native.process_group_options(), **kwargs
                )
            )
            resources.callback(_reap, process)
            tree = _native.own_process(process)
            owner = OwnedProcess(process, tree)
            resources.callback(owner.stop, timeout=0)
        yield owner


def _reap(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.kill()
    process.wait()


def is_interrupted(returncode: int | None) -> bool:
    return returncode in (130, -signal.SIGINT, 0xC000013A)


@contextmanager
def ignore_interrupts() -> Iterator[None]:
    previous = {
        sig: signal.signal(sig, signal.SIG_IGN) for sig in _native.interrupt_signals()
    }
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


@contextmanager
def cancel_on_interrupt(cancelled: Event) -> Iterator[None]:
    """Let supervising threads stop children before the main thread unwinds."""

    previous_path = os.environ.get(_CANCEL_ENV)
    directory = None
    if _native.CANCELLATION_MARKER_REQUIRED and previous_path is None:
        directory = tempfile.TemporaryDirectory(prefix="physiclaw-cancel-")
        path = Path(directory.name) / "cancel"
    elif previous_path:
        path = Path(previous_path)
    else:
        path = None
    if path is not None:
        os.environ[_CANCEL_ENV] = str(path)

    def cancel(*_: Any) -> None:
        # Event.set holds a non-reentrant lock. A second Ctrl+C must not
        # re-enter it while the first signal is setting cancellation.
        for sig in _native.interrupt_signals():
            signal.signal(sig, signal.SIG_IGN)
        cancelled.set()
        if path is not None:
            path.touch()

    cancelled.clear()
    previous = {sig: signal.signal(sig, cancel) for sig in _native.interrupt_signals()}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        cancelled.clear()
        if previous_path is None:
            os.environ.pop(_CANCEL_ENV, None)
        if directory is not None:
            directory.cleanup()
