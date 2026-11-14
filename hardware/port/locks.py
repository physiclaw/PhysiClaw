"""Stable OS lock files and explicit lock inheritance for owned children."""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from hardware.port._select import native as _native


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Acquire without waiting; contention raises BlockingIOError."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        _native.lock_stream(stream)
        yield


@contextmanager
def output_lock(path: Path) -> Iterator[None]:
    """Hold or inherit the coordinator's lock until all work has stopped.

    The scoped environment value is a child-process protocol. Its native
    descriptor/handle is never consumed by build workflows themselves.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with _native.output_lock(path):
        yield


@contextmanager
def child_inheritance() -> Iterator[dict[str, Any]]:
    """Limit inheritable handles to the protected process-creation window."""
    with _native.child_inheritance() as options:
        yield options


@contextmanager
def without_output_lock_inheritance() -> Iterator[None]:
    """Run read-only help without forwarding a live or stale output lease."""
    inherited = {
        key: os.environ.pop(key)
        for key in ("PHYSICLAW_HW_LOCK_FD", "PHYSICLAW_HW_LOCK_HANDLE")
        if key in os.environ
    }
    try:
        yield
    finally:
        os.environ.update(inherited)
