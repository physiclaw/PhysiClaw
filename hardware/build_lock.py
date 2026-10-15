"""One writer per output tree; OS locks are released even after a crash.

Child workers inherit the open descriptor, so they share the dispatcher's
lock. A separate CLI invocation cannot accidentally interleave its writes.
The CAD pipeline supports macOS and Linux (both provide flock).
"""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from functools import wraps

from hardware.scheme import OUTPUT_DIR

_ENV = "PHYSICLAW_HW_LOCK_FD"


def inherited_fds() -> tuple[int, ...]:
    try:
        fd = int(os.environ[_ENV])
        os.fstat(fd)
        return (fd,)
    except (KeyError, ValueError, OSError):
        return ()


@contextmanager
def build_lock():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / ".build.lock"
    with path.open("a+") as lock:
        inherited = inherited_fds()
        if inherited and os.path.samestat(
            os.fstat(inherited[0]), os.fstat(lock.fileno())
        ):
            yield
            return
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(
                f"Hardware build already running in {OUTPUT_DIR}"
            ) from None
        previous = os.environ.get(_ENV)
        os.environ[_ENV] = str(lock.fileno())
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop(_ENV, None)
            else:
                os.environ[_ENV] = previous
            # Do not unlink the lock file: waiters could otherwise lock a
            # different inode and both think they own this output tree.


def locked_main(func):
    @wraps(func)
    def run(*args, **kwargs):
        try:
            with build_lock():
                return func(*args, **kwargs)
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from None

    return run
