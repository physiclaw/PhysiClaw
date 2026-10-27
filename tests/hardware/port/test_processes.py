"""Owned child trees must release pipes even after their leader exits."""

import subprocess
import sys

import pytest

from hardware.port import processes
from hardware.port.backends import posix
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


def test_posix_cleanup_reaps_exited_leader_after_signal_denied(mocker):
    # Exercise the POSIX backend even on Windows, which has no SIGKILL.
    mocker.patch.object(posix.signal, "SIGKILL", 9, create=True)
    reaped = False

    def wait(timeout=None):
        nonlocal reaped
        reaped = True
        return 0

    def signal_group(pid, sig):
        if not reaped:
            raise PermissionError("group contains only an unreaped child")
        raise ProcessLookupError("reaping removed the empty group")

    process = mocker.Mock(pid=1234)
    process.wait.side_effect = wait
    mocker.patch.object(posix.os, "killpg", side_effect=signal_group, create=True)

    posix.ProcessTree(process).stop(timeout=0)

    assert reaped
    process.wait.assert_called()


@pytest.mark.parametrize("returncode", [None, 0])
def test_posix_cleanup_preserves_permission_errors_for_existing_group(
    mocker, returncode
):
    mocker.patch.object(posix.signal, "SIGKILL", 9, create=True)
    process = mocker.Mock(pid=1234)
    if returncode is None:
        process.wait.side_effect = subprocess.TimeoutExpired("child", 1)
    else:
        process.wait.return_value = returncode
    mocker.patch.object(
        posix.os,
        "killpg",
        side_effect=PermissionError("cannot signal live group members"),
        create=True,
    )

    with pytest.raises(PermissionError, match="cannot signal live group members"):
        posix.ProcessTree(process).stop(timeout=0)
