"""Owned child trees must release pipes even after their leader exits."""

import subprocess
import sys

import pytest

from hardware.port import processes
from hardware.port.processes import owned_process


def test_cleanup_reaps_descendant_after_leader_exits(tmp_path):
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    helper = (
        "from pathlib import Path; import time; "
        f"Path({str(ready)!r}).touch(); time.sleep(60)"
    )
    script = (
        "import subprocess, sys, time; from pathlib import Path\n"
        f"while not Path({str(release)!r}).exists(): time.sleep(0.01)\n"
        f"subprocess.Popen([sys.executable, '-c', {helper!r}])\n"
        f"while not Path({str(ready)!r}).exists(): time.sleep(0.01)\n"
    )
    with owned_process(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as owner:
        process = owner.process
        try:
            release.touch()
            assert process.wait(timeout=10) == 0

            owner.stop(timeout=0)

            # POSIX descendants may log KeyboardInterrupt during SIGINT.
            # The bounded EOF, rather than cancellation diagnostics, matters.
            output, _ = process.communicate(timeout=5)
            assert output == b""
        finally:
            owner.stop(timeout=0)


def test_body_failure_stops_child_and_closes_pipes():
    with pytest.raises(ValueError, match="collection failed"):
        with owned_process(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as owner:
            process = owner.process

            raise ValueError("collection failed")

    assert process.poll() is not None
    assert process.stdout.closed and process.stderr.closed


def test_native_setup_failure_reaps_child_and_closes_pipes(mocker):
    started = []

    def refuse(process):
        started.append(process)
        raise OSError("native ownership failed")

    mocker.patch.object(processes._native, "own_process", side_effect=refuse)

    with pytest.raises(OSError, match="native ownership failed"):
        with owned_process(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ):
            pytest.fail("setup failed before entering body")

    process = started[0]
    assert process.poll() is not None
    assert process.stdout.closed and process.stderr.closed
