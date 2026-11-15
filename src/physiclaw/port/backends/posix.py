"""POSIX implementations of application desktop, process, file, and lock operations."""

import importlib
import os
import shlex
import signal
import subprocess
from pathlib import Path
from typing import BinaryIO

REMOTE_SHUTDOWN_SUPPORTED = True
EDITOR_CANDIDATES = ("vim", "vi", "nano")


def run_editor(editor: str | list[str], filename: str) -> None:
    argv = shlex.split(editor) if isinstance(editor, str) else editor
    subprocess.run([*argv, filename], check=True)


def termination_signals() -> tuple[int, ...]:
    return (signal.SIGTERM,)


def process_group_options() -> dict:
    return {"start_new_session": True}


def prepare_managed_runtime() -> None:
    """POSIX needs no preload before starting the managed control reader."""


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    return True


def request_shutdown(pid: int) -> None:
    os.kill(pid, signal.SIGINT)


def native_path(path: Path) -> Path:
    return path


def replace_file(source: Path, target: Path) -> None:
    source.replace(target)


def lock_stream(stream: BinaryIO) -> None:
    fcntl = importlib.import_module("fcntl")
    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)


def remove_readonly(function, path, error) -> None:
    raise error
