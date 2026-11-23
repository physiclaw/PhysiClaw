"""Run the installed dryrun command with a guard in every Python process."""

import os
import shutil
import subprocess
import sys
import tempfile
from functools import partial
from pathlib import Path
from typing import NoReturn


def _install_guard(root: Path) -> None:
    def reject(operation: str, /, *args, **kwargs) -> NoReturn:
        # Keep evidence even if the application catches the exception.
        with (root / "violations.log").open("a", encoding="utf-8") as log:
            print(operation, file=log)
        raise AssertionError(f"dryrun attempted {operation}")

    def audit(event: str, args: tuple) -> None:
        if event == "socket.connect" and isinstance(args[1], tuple):
            host = args[1][0]
        elif event == "socket.getaddrinfo":
            host = args[0]
        else:
            return
        if host not in (None, "127.0.0.1", "::1", "localhost"):
            reject(f"non-loopback network access to {host!r}")

    sys.addaudithook(audit)

    import cv2
    import serial
    from serial.tools import list_ports

    from physiclaw import port

    cv2.VideoCapture = partial(reject, "native camera capture")
    serial.Serial = partial(reject, "native serial port open")
    list_ports.comports = partial(reject, "native serial port discovery")
    for name in port.__all__:
        if name == "ensure_camera_permission" or (
            name.startswith("camera_") and name != "camera_capture_lock"
        ):
            setattr(port, name, partial(reject, f"native camera operation {name}"))

    argv = sys.orig_argv
    role = argv[argv.index("-m") + 1] if "-m" in argv else "cli"
    with (root / "guarded.log").open("a", encoding="utf-8") as log:
        print(role, file=log)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="physiclaw-dryrun-guard-") as temp:
        root = Path(temp)
        # Python imports sitecustomize in the CLI, worker, and managed agent.
        shutil.copyfile(__file__, root / "sitecustomize.py")
        result = subprocess.run(
            [sys.executable, "-c", "from physiclaw.cli import app; app()", "dryrun"],
            env={**os.environ, "PYTHONPATH": str(root)},
            timeout=300,
            check=False,
        )
        violations = root / "violations.log"
        if violations.exists():
            sys.stderr.write(violations.read_text(encoding="utf-8"))
            return 1
        if result.returncode:
            return result.returncode
        guarded = root / "guarded.log"
        expected = {"cli", "physiclaw.dryrun", "physiclaw.agent.runtime"}
        active = (
            set(guarded.read_text(encoding="utf-8").splitlines())
            if guarded.exists()
            else set()
        )
        if missing := expected - active:
            print(
                f"Missing dryrun I/O guard in {', '.join(sorted(missing))}",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "sitecustomize":
    try:
        if os.environ.get("COVERAGE_PROCESS_START"):
            from coverage import process_startup

            process_startup()
        _install_guard(Path(__file__).parent)
    except Exception as exc:
        # Python normally reports sitecustomize errors and continues startup.
        sys.exit(f"Could not install the dryrun I/O guard: {exc}")
elif __name__ == "__main__":
    sys.exit(main())
