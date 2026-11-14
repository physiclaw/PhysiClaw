"""One writer per hardware output tree; native locking lives in hardware.port."""

import sys
from collections.abc import Callable, Iterator
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


def locked_main[**P, R](func: Callable[P, R]) -> Callable[P, R]:
    """Lock CLI work while keeping read-only help available."""

    @wraps(func)
    def run(*args: P.args, **kwargs: P.kwargs) -> R:
        # CLI help must remain available while a build owns the output tree.
        # Only recognize options before `--`, which ends option parsing.
        supplied = kwargs.get("argv", args[0] if args else None)
        argv = sys.argv[1:] if supplied is None else supplied
        if isinstance(argv, (list, tuple)):
            options = list(argv)
            if "--" in options:
                options = options[: options.index("--")]
            if "-h" in options or "--help" in options or options[:1] == ["help"]:
                with locks.without_output_lock_inheritance():
                    return func(*args, **kwargs)
        try:
            with build_lock():
                return func(*args, **kwargs)
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from None

    return run
