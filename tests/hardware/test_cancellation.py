from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

import pytest


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
finished = Path({str(output)!r}, 'step', f'{{os.getpid()}}.step')
finished.parent.mkdir(parents=True, exist_ok=True)
finished.write_text('completed output')
Path({str(control)!r}, f'{{os.getpid()}}.worker').write_text(str(os.getpgrp()))
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
    with subprocess.Popen(
        [sys.executable, "-u", "-c", wrapper],
        env=env,
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    ) as process:
        try:
            deadline = time.monotonic() + 15
            while len(list(control.glob("*.worker"))) < expected_workers:
                if process.poll() is not None or time.monotonic() >= deadline:
                    pytest.fail(
                        "Workers did not start: " + process.communicate(timeout=1)[0]
                    )
                time.sleep(0.02)
            # A terminal sends SIGINT to the entire foreground process group,
            # reaching both the CLI wrapper and its dispatcher at once.
            os.killpg(process.pid, signal.SIGINT)
            if stubborn:
                time.sleep(0.05)
                os.killpg(process.pid, signal.SIGINT)
            terminal, _ = process.communicate(timeout=8)
            remaining = []
            for marker in control.glob("*.worker"):
                try:
                    os.kill(int(marker.stem), 0)
                    remaining.append(marker.stem)
                except ProcessLookupError:
                    pass
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            # Always clean up owned test workers, including on assertion failure.
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
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
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
