"""Process locks with optional bounded waiting and handle-scoped lifetime."""

import math
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from physiclaw.port._select import native as _native


class FileLockBusy(RuntimeError):
    """Another process or thread holds the requested lock."""


@contextmanager
def exclusive_file_lock(path: Path, *, timeout: float = 0) -> Iterator[None]:
    """Hold a stable lock file until exit; never unlink it while callers may wait.

    The OS releases the lock when the handle closes, including process exit.
    Parent directories are created as needed. Contention raises FileLockBusy
    immediately by default, or after `timeout` seconds when waiting is requested.
    Only acquisition is retried; exceptions from the body always propagate.
    """
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("timeout must be finite and nonnegative")
    deadline = time.monotonic() + timeout
    with ExitStack() as stack:
        while True:
            try:
                stack.enter_context(_try_lock(path))
                break
            except FileLockBusy:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(0.01, remaining))
        yield


@contextmanager
def _try_lock(path: Path) -> Iterator[None]:
    """Acquire once; close the handle on failure or when the holder exits."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock:
        try:
            _native.lock_stream(lock)
        except BlockingIOError as exc:
            raise FileLockBusy(f"lock is held: {path}") from exc
        yield
