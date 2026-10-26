"""Windows mechanisms for hardware tooling, including explicit resource owners."""

import importlib
import os
import signal
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Any, BinaryIO, TextIO

from hardware.port.backends._win32 import own_process as own_process

CANCELLATION_MARKER_REQUIRED = True
_HANDLE_ENV = "PHYSICLAW_HW_LOCK_HANDLE"
# Popen runs from several worker threads. Serialize the short inheritability
# window so one spawn cannot restore a handle flag during another spawn.
_SPAWN_LOCK = Lock()


def process_group_options() -> dict[str, Any]:
    return {
        "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP") | 0x00000004
    }


def interrupt_signals() -> tuple[int, ...]:
    return (signal.SIGINT, getattr(signal, "SIGBREAK"))


def lock_stream(stream: BinaryIO) -> None:
    msvcrt = importlib.import_module("msvcrt")
    if stream.tell() == 0:
        stream.write(b"\0")
        stream.flush()
    stream.seek(0)
    try:
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        raise BlockingIOError(str(exc)) from exc


@contextmanager
def _inherited_lock() -> Iterator[tuple[BinaryIO, int] | None]:
    """Own a validated duplicate before a new open can reuse a stale handle."""
    msvcrt = importlib.import_module("msvcrt")
    winapi = importlib.import_module("_winapi")
    inherited = os.environ.get(_HANDLE_ENV)
    if inherited is None:
        yield None
        return
    try:
        handle = int(inherited)
        process = winapi.GetCurrentProcess()
        duplicate = winapi.DuplicateHandle(
            process, handle, process, 0, False, winapi.DUPLICATE_SAME_ACCESS
        )
    except (OSError, ValueError):
        yield None
        return
    try:
        fd = msvcrt.open_osfhandle(duplicate, os.O_RDWR)
    except OSError:
        winapi.CloseHandle(duplicate)
        yield None
        return
    # Only metadata is needed. An unbuffered wrapper also accepts a pipe
    # whose recycled handle must fail the file-identity check below.
    with os.fdopen(fd, "rb", buffering=0) as owned:
        yield owned, handle


@contextmanager
def output_lock(path: Path) -> Iterator[None]:
    msvcrt = importlib.import_module("msvcrt")
    # Enter the duplicate's scope first: opening the lock file first could
    # give a non-inheriting child exactly the stale advertised handle value.
    with _inherited_lock() as inherited, path.open("a+b") as stream:
        if inherited is not None:
            owned, handle = inherited
            if os.path.samestat(os.fstat(owned.fileno()), os.fstat(stream.fileno())):
                getattr(os, "set_handle_inheritable")(handle, False)
                yield
                return
        lock_stream(stream)
        handle = msvcrt.get_osfhandle(stream.fileno())
        previous = os.environ.get(_HANDLE_ENV)
        os.environ[_HANDLE_ENV] = str(handle)
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop(_HANDLE_ENV, None)
            else:
                os.environ[_HANDLE_ENV] = previous


@contextmanager
def child_inheritance() -> Iterator[dict[str, Any]]:
    handle = os.environ.get(_HANDLE_ENV)
    if handle is None:
        yield {}
        return
    with _SPAWN_LOCK:
        value = int(handle)
        previous = getattr(os, "get_handle_inheritable")(value)
        getattr(os, "set_handle_inheritable")(value, True)
        try:
            startup = getattr(subprocess, "STARTUPINFO")()
            startup.lpAttributeList = {"handle_list": [value]}
            yield {"startupinfo": startup, "close_fds": True}
        finally:
            getattr(os, "set_handle_inheritable")(value, previous)


def link_directory(source: Path, target: Path) -> None:
    winapi = importlib.import_module("_winapi")
    winapi.CreateJunction(str(source), str(target))


def browser_locations() -> list[Path]:
    locations = []
    for name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        root = os.environ.get(name)
        if not root:
            continue
        for relative in (
            "Google/Chrome/Application/chrome.exe",
            "Microsoft/Edge/Application/msedge.exe",
            "Chromium/Application/chrome.exe",
        ):
            locations.append(Path(root) / relative)
    return locations


@contextmanager
def key_input(stream: TextIO) -> Iterator[None]:
    yield


def read_key(stream: TextIO) -> str:
    msvcrt = importlib.import_module("msvcrt")
    key = msvcrt.getwch()
    if key in ("\x00", "\xe0"):
        msvcrt.getwch()
        return "special"
    return key
