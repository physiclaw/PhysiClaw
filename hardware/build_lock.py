"""One writer per hardware output tree; native locking lives in hardware.port."""

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from functools import wraps

from hardware.port import locks
from hardware.scheme import OUTPUT_DIR


@contextmanager
def build_lock() -> Iterator[None]:
    with ExitStack() as resources:
        try:
            resources.enter_context(locks.output_lock(OUTPUT_DIR / ".build.lock"))
        except BlockingIOError:
            raise RuntimeError(
                f"Hardware build already running in {OUTPUT_DIR}"
            ) from None
        yield


def locked_main(func):
    @wraps(func)
    def run(*args, **kwargs):
        try:
            with build_lock():
                return func(*args, **kwargs)
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from None

    return run
