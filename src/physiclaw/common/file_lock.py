"""Nonblocking process locks whose lifetime follows an open file handle."""

import importlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class FileLockBusy(RuntimeError):
    """Another process or thread holds the requested lock."""


@contextmanager
def exclusive_file_lock(path: Path) -> Iterator[None]:
    """Hold a stable lock file until exit; never unlink it while callers may wait.

    The OS releases the lock when the handle closes, including process exit.
    Parent directories are created as needed. Contention raises FileLockBusy.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock:
        try:
            import fcntl
        except ImportError:  # Windows
            msvcrt = importlib.import_module("msvcrt")
            if lock.tell() == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as e:
                raise FileLockBusy(f"lock is held: {path}") from e
        else:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as e:
                raise FileLockBusy(f"lock is held: {path}") from e
        yield
