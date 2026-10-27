"""POSIX mechanisms for hardware tooling; no build or application policy."""

import codecs
import importlib
import os
import select
import signal
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, TextIO

CANCELLATION_MARKER_REQUIRED = False
_LOCK_FD_ENV = "PHYSICLAW_HW_LOCK_FD"


def process_group_options() -> dict[str, Any]:
    return {"start_new_session": True}


def interrupt_signals() -> tuple[int, ...]:
    return (signal.SIGINT,)


class ProcessTree:
    def __init__(self, process: subprocess.Popen):
        self.process = process

    def stop(self, timeout: float, *, cooperative_cancel: bool = False) -> None:
        def send(sig: int) -> None:
            try:
                try:
                    getattr(os, "killpg")(self.process.pid, sig)
                except PermissionError as exc:
                    # macOS can return EPERM while the leader is still exiting.
                    # Reap it with a bounded wait, then signal any descendants.
                    # A live leader or a denied retry is a real permission error.
                    try:
                        self.process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        raise exc
                    getattr(os, "killpg")(self.process.pid, sig)
            except ProcessLookupError:
                pass

        try:
            for sig, wait in ((signal.SIGINT, timeout), (signal.SIGTERM, 1)):
                send(sig)
                try:
                    self.process.wait(timeout=wait)
                    return
                except subprocess.TimeoutExpired:
                    pass
        finally:
            # Descendants can still own pipes after the group leader exits.
            send(getattr(signal, "SIGKILL"))
            self.process.wait()


def lock_stream(stream: BinaryIO) -> None:
    fcntl = importlib.import_module("fcntl")
    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _inherited_fd() -> int | None:
    try:
        fd = int(os.environ[_LOCK_FD_ENV])
        os.fstat(fd)
        return fd
    except (KeyError, ValueError, OSError):
        return None


@contextmanager
def output_lock(path: Path) -> Iterator[None]:
    # Validate the inherited descriptor before opening anything that could
    # reuse its number in a child that did not inherit it.
    inherited = _inherited_fd()
    with path.open("a+b") as stream:
        if inherited is not None and os.path.samestat(
            os.fstat(inherited), os.fstat(stream.fileno())
        ):
            yield
            return
        lock_stream(stream)
        previous = os.environ.get(_LOCK_FD_ENV)
        os.environ[_LOCK_FD_ENV] = str(stream.fileno())
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop(_LOCK_FD_ENV, None)
            else:
                os.environ[_LOCK_FD_ENV] = previous


@contextmanager
def child_inheritance() -> Iterator[dict[str, Any]]:
    fd = _inherited_fd()
    yield {"pass_fds": (fd,) if fd is not None else ()}


def link_directory(source: Path, target: Path) -> None:
    target.symlink_to(source, target_is_directory=True)


def browser_locations() -> list[Path]:
    return [
        Path(f"/Applications/{app}.app/Contents/MacOS/{executable}")
        for app, executable in (
            ("Google Chrome", "Google Chrome"),
            ("Chromium", "Chromium"),
            ("Microsoft Edge", "Microsoft Edge"),
        )
    ]


@contextmanager
def key_input(stream: TextIO) -> Iterator[None]:
    termios = importlib.import_module("termios")
    tty = importlib.import_module("tty")
    fd = stream.fileno()
    settings = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, settings)


def read_key(stream: TextIO) -> str:
    fd = stream.fileno()
    decoder = codecs.getincrementaldecoder(stream.encoding or "utf-8")("replace")

    def character() -> str:
        while chunk := os.read(fd, 1):
            if text := decoder.decode(chunk):
                return text
        return ""

    key = character()
    if key != "\x1b" or not select.select([fd], [], [], 0.05)[0]:
        return key
    following = character()
    if following == "\x1b":
        return following
    if following in {"[", "O"}:
        while select.select([fd], [], [], 0.05)[0]:
            if "@" <= character() <= "~":
                break
    return "special"


def own_process(process: subprocess.Popen) -> ProcessTree:
    return ProcessTree(process)
