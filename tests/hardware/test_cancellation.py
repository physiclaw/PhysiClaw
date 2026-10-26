from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from hardware.port.processes import owned_process


@pytest.mark.parametrize("returncode", [130, -signal.SIGINT, 0xC000013A])
def test_regression_cli_normalizes_interrupted_child_status(
    tmp_path, mocker, returncode
):
    from contextlib import nullcontext
    from types import SimpleNamespace

    from hardware import __main__ as cli

    child = SimpleNamespace(wait=lambda **kwargs: returncode)
    owner = SimpleNamespace(process=child)
    mocker.patch.object(cli, "owned_process", return_value=nullcontext(owner))
    mocker.patch("hardware.build_lock.OUTPUT_DIR", tmp_path)

    result = cli.main(["build"])

    assert result == 130


def _running(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            status = wintypes.DWORD()
            return (
                bool(kernel.GetExitCodeProcess(handle, ctypes.byref(status)))
                and status.value == 259
            )
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


@pytest.mark.parametrize(
    "mode,stubborn", [("dispatch", False), ("dispatch", True), ("single", True)]
)
def test_ctrl_c_cleans_process_tree_and_saves_cancelled_result(
    tmp_path, mode, stubborn
):
    pytest.importorskip("build123d", reason="CAD dependencies required")
    control = tmp_path / "control"
    control.mkdir()
    output = tmp_path / "output"
    worker = f"""
import os, signal, time
from pathlib import Path
signal.signal(signal.SIGINT, {"signal.SIG_IGN" if stubborn else "signal.default_int_handler"})
signal.signal(signal.SIGTERM, {"signal.SIG_IGN" if stubborn else "signal.SIG_DFL"})
if os.name == "nt":
    signal.signal(signal.SIGBREAK, {"signal.SIG_IGN" if stubborn else "signal.default_int_handler"})
finished = Path({str(output)!r}, 'step', f'{{os.getpid()}}.step')
finished.parent.mkdir(parents=True, exist_ok=True)
finished.write_text('completed output')
Path({str(control)!r}, f'{{os.getpid()}}.worker').write_text(str(os.getpid()))
time.sleep(60)
"""
    args = (
        ["--no-cache", "--batch-size", "1", "--jobs", "2"]
        if mode == "dispatch"
        else ["--stems", "frame_10"]
    )
    dispatcher = f"""
import subprocess, sys
from hardware.assembly import build_procedures as bp
original_popen = subprocess.Popen
def slow_worker(argv, **kwargs):
    return original_popen([sys.executable, '-u', '-c', {worker!r}], **kwargs)
bp.subprocess.Popen = slow_worker
bp._batches = lambda size: [[f'frame_{{i}}'] for i in range(12)]
bp._incomplete = lambda stem, flags: ['assembled']
bp.stepcache.source_key = lambda stem: stem
bp.stepcache.annotation_key = lambda stem: stem
raise SystemExit(bp.main({args!r}))
"""
    wrapper = f"""
from hardware import __main__ as cli
cli._DELEGATED['build'] = ['-u', '-c', {dispatcher!r}]
raise SystemExit(cli.main(['build']))
"""
    expected_workers = 2 if mode == "dispatch" else 1
    env = {**os.environ, "PHYSICLAW_HW_OUTPUT": str(output)}
    with owned_process(
        [sys.executable, "-u", "-c", wrapper],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    ) as owner:
        process = owner.process
        try:
            deadline = time.monotonic() + 60
            while len(list(control.glob("*.worker"))) < expected_workers:
                if process.poll() is not None or time.monotonic() >= deadline:
                    pytest.fail(
                        "Workers did not start: " + process.communicate(timeout=1)[0]
                    )
                time.sleep(0.02)
            # A terminal sends SIGINT to the entire foreground process group,
            # reaching both the CLI wrapper and its dispatcher at once.
            process.send_signal(
                signal.CTRL_BREAK_EVENT
            ) if os.name == "nt" else os.killpg(process.pid, signal.SIGINT)
            if stubborn:
                time.sleep(0.05)
                process.send_signal(
                    signal.CTRL_BREAK_EVENT
                ) if os.name == "nt" else os.killpg(process.pid, signal.SIGINT)
            # The CLI allows 10s for dispatcher cleanup, then 1s for SIGTERM.
            # Leave scheduling headroom on busy CI runners.
            terminal, _ = process.communicate(timeout=15)
            remaining = [
                marker.stem
                for marker in control.glob("*.worker")
                if _running(int(marker.stem))
            ]
        finally:
            owner.stop(timeout=0)
            if os.name != "nt":
                for marker in control.glob("*.worker"):
                    try:
                        os.killpg(int(marker.stem), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
    assert process.returncode == 130
    assert remaining == []
    assert "CANCELLED" in terminal
    assert "Traceback" not in terminal and "KeyboardInterrupt" not in terminal
    assert "FAILED" not in terminal and "SUCCESS" not in terminal
    workers = list(control.glob("*.worker"))
    assert len(workers) == expected_workers  # No queued batch or retry was launched.
    assert len(list((output / "step").glob("*.step"))) == expected_workers
    for marker in workers:
        pid = int(marker.stem)
        assert int(marker.read_text()) == pid  # Each worker owned a private group.
        assert not _running(pid)
    events = [
        json.loads(line)
        for line in next((output / "logs").glob("*.jsonl")).read_text().splitlines()
    ]
    assert events[-1]["event"] == "build_finished"
    assert events[-1]["status"] == "cancelled"
    assert events[-1]["exit_code"] == 130
    assert len([e for e in events if e["event"] == "build_finished"]) == 1
    assert not any(e["event"] == "step_retry" for e in events)
    interrupted = next(e for e in events if e["event"] == "build_interrupted")
    assert "KeyboardInterrupt" in interrupted["traceback"]
    # The wrapper's output lock must have been released along with its children.
    acquired = subprocess.run(
        [
            sys.executable,
            "-c",
            "from hardware.build_lock import build_lock\nwith build_lock(): print('released')",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert acquired.returncode == 0, acquired.stderr
    assert acquired.stdout.strip() == "released"


def test_repeated_ctrl_c_during_cancellation_reporting(tmp_path):
    pytest.importorskip("build123d", reason="CAD dependencies required")
    script = """
import signal
from hardware.assembly import build_procedures as bp

interrupted = []

def interrupt_at(name):
    if stage == name:
        interrupted.append(name)
        # The wrapper can forward SIGINT after the dispatcher has already
        # handled the terminal's signal and stopped all its workers.
        signal.raise_signal(signal.SIGINT)

def execute(args):
    signal.raise_signal(signal.SIGINT)
    bp._check_cancelled()

format_exc = bp.traceback.format_exc
def report_traceback():
    interrupt_at('traceback')
    return format_exc()

event = bp.buildlog.event
def report_event(name, *args, **kwargs):
    interrupt_at(name)
    return event(name, *args, **kwargs)

close = bp.buildlog.BuildLog.__exit__
def close_log(self, *args):
    interrupt_at('log_close')
    return close(self, *args)

bp._execute = execute
bp.traceback.format_exc = report_traceback
bp.buildlog.event = report_event
bp.buildlog.BuildLog.__exit__ = close_log
previous = signal.getsignal(signal.SIGINT)
stages = ['traceback', 'build_finished', 'log_close']
# Exercise each phase with its own second SIGINT, importing CAD only once.
for stage in stages:
    rc = bp.main([])
    assert rc == 130, stage
    assert signal.getsignal(signal.SIGINT) == previous, stage
    assert not bp._CANCELLED.is_set(), stage
assert interrupted == stages
raise SystemExit(rc)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "PHYSICLAW_HW_OUTPUT": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=15,
    )
    terminal = result.stdout + result.stderr
    assert result.returncode == 130, terminal
    assert result.stdout.count("CANCELLED") == 3
    assert "Traceback" not in terminal and "KeyboardInterrupt" not in terminal
    logs = list((tmp_path / "logs").glob("*.jsonl"))
    assert len(logs) == 3
    for log in logs:
        events = [json.loads(line) for line in log.read_text().splitlines()]
        interrupted = [e for e in events if e["event"] == "build_interrupted"]
        assert len(interrupted) == 1
        assert "KeyboardInterrupt" in interrupted[0]["traceback"]
        finished = [e for e in events if e["event"] == "build_finished"]
        assert finished == [events[-1]]
        assert finished[0]["status"] == "cancelled"
        assert finished[0]["exit_code"] == 130


def test_repeated_ctrl_c_cannot_reenter_cancellation_event():
    script = """
import signal
from threading import Event
from hardware.port.processes import cancel_on_interrupt

cancelled = Event()
notify = cancelled._cond.notify_all
def interrupt_again():
    # Event.set holds its non-reentrant condition lock while notifying.
    signal.raise_signal(signal.SIGINT)
    notify()
cancelled._cond.notify_all = interrupt_again

previous = signal.getsignal(signal.SIGINT)
with cancel_on_interrupt(cancelled):
    signal.raise_signal(signal.SIGINT)
    assert cancelled.is_set()
    signal.raise_signal(signal.SIGINT)
    assert cancelled.is_set()
assert signal.getsignal(signal.SIGINT) == previous
assert not cancelled.is_set()
print('cancelled without deadlock')
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "cancelled without deadlock"


@pytest.mark.parametrize("finishes", [False, True])
def test_ctrl_c_during_wait_lock_acquisition_does_not_leak_lock(finishes):
    script = f"""
import signal, subprocess
from threading import Lock
from hardware import __main__ as cli

class Child:
    returncode = None
    lock = Lock()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.wait()

    def wait(self, timeout=None):
        if self.returncode is not None:
            return self.returncode
        assert self.lock.acquire(False), 'waitpid lock leaked'
        # Reproduce SIGINT after waitpid-lock acquisition, before the protected
        # block starts. An asynchronous KeyboardInterrupt would leak this lock.
        signal.raise_signal(signal.SIGINT)
        try:
            if {finishes!r}:
                self.returncode = 0
                return 0
            raise subprocess.TimeoutExpired('child', timeout)
        finally:
            self.lock.release()

def cleanup(child, **kwargs):
    assert child.lock.acquire(False), 'waitpid lock leaked'
    child.lock.release()
    child.returncode = 130

from contextlib import contextmanager
from types import SimpleNamespace

@contextmanager
def owned(*args, **kwargs):
    child = Child()
    yield SimpleNamespace(process=child, stop=lambda **kw: cleanup(child, **kw))

cli.owned_process = owned
assert cli._delegate(['-c', ''], []) == 130
print('cancelled without leaking the waitpid lock')
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "cancelled without leaking the waitpid lock"


def test_ctrl_c_during_delegated_import_has_no_traceback(tmp_path):
    (tmp_path / "interrupted_stage.py").write_text("raise KeyboardInterrupt\n")
    script = (
        "from hardware import __main__ as cli\n"
        "cli._DELEGATED['build'] = ['-m', 'interrupted_stage']\n"
        "raise SystemExit(cli.main(['build']))\n"
    )
    env = {
        **os.environ,
        "PHYSICLAW_HW_OUTPUT": str(tmp_path / "output"),
        "PYTHONPATH": str(tmp_path) + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 130
    assert result.stderr.strip() == "CANCELLED"
    assert "Traceback" not in result.stdout + result.stderr


@pytest.mark.skipif(os.name != "nt", reason="Windows cancellation marker protocol")
@pytest.mark.parametrize("command", ["manual", "release"])
def test_ctrl_c_stops_delegated_command_that_does_not_poll_marker(tmp_path, command):
    ready = tmp_path / "ready"
    written = tmp_path / "written-after-cancellation"
    (tmp_path / "uncooperative_stage.py").write_text(
        "import time\nfrom pathlib import Path\n"
        f"Path({str(ready)!r}).touch()\n"
        "time.sleep(3)\n"
        f"Path({str(written)!r}).touch()\n",
        encoding="utf-8",
    )
    script = f"""
import signal, threading, time
from pathlib import Path
from hardware import __main__ as cli

def interrupt_when_ready():
    while not Path({str(ready)!r}).exists():
        time.sleep(0.01)
    signal.raise_signal(signal.SIGINT)

cli._DELEGATED[{command!r}] = ['-m', 'uncooperative_stage']
threading.Thread(target=interrupt_when_ready, daemon=True).start()
raise SystemExit(cli.main([{command!r}]))
"""
    env = {
        **os.environ,
        "PHYSICLAW_HW_OUTPUT": str(tmp_path / "output"),
        "PYTHONPATH": str(tmp_path) + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }
    # Release preparation can run this suite with its own cancellation marker.
    env.pop("PHYSICLAW_HW_CANCEL_FILE", None)
    with owned_process(
        [sys.executable, "-c", script],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as owner:
        stdout, stderr = owner.process.communicate(timeout=15)

    assert owner.process.returncode == 130, stdout + stderr
    assert ready.exists()
    assert not written.exists(), "delegated command kept working after Ctrl+C"
