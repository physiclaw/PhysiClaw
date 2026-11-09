from __future__ import annotations

import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from hardware import build_lock as lock
from hardware.port import locks


def test_separate_process_is_rejected_but_child_can_inherit(tmp_path, monkeypatch):
    monkeypatch.setattr(lock, "OUTPUT_DIR", tmp_path)
    env = {**os.environ, "PHYSICLAW_HW_OUTPUT": str(tmp_path)}
    script = (
        "from hardware.build_lock import build_lock\nwith build_lock(): print('locked')"
    )
    with lock.build_lock():
        env.update(os.environ)
        env["PHYSICLAW_HW_OUTPUT"] = str(tmp_path)
        rejected = subprocess.run(
            [sys.executable, "-c", script], env=env, capture_output=True
        )
        assert rejected.returncode != 0 and b"already running" in rejected.stderr
        with locks.child_inheritance() as options:
            accepted = subprocess.run(
                [sys.executable, "-c", script],
                env=env,
                capture_output=True,
                **options,
            )
        assert accepted.returncode == 0, accepted.stderr
    accepted = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True
    )
    assert accepted.returncode == 0, accepted.stderr


def test_exception_releases_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(lock, "OUTPUT_DIR", tmp_path)
    with pytest.raises(ValueError, match="interrupted"), lock.build_lock():
        raise ValueError("interrupted")
    with lock.build_lock():
        with locks.child_inheritance() as options:
            assert options


def test_body_io_error_is_not_reported_as_lock_contention(tmp_path, monkeypatch):
    monkeypatch.setattr(lock, "OUTPUT_DIR", tmp_path)

    with pytest.raises(BlockingIOError, match="worker pipe blocked"):
        with lock.build_lock():
            raise BlockingIOError("worker pipe blocked")


def test_reused_closed_handle_cannot_bypass_output_lock(tmp_path):
    path = tmp_path / "output.lock"
    script = """
import os, sys
from pathlib import Path
from hardware.port.locks import output_lock
path = Path(sys.argv[1])
with path.open('a+b') as probe:
    if os.name == 'nt':
        import msvcrt
        key, value = 'PHYSICLAW_HW_LOCK_HANDLE', msvcrt.get_osfhandle(probe.fileno())
    else:
        key, value = 'PHYSICLAW_HW_LOCK_FD', probe.fileno()
os.environ[key] = str(value)
with output_lock(path):
    print('independent writer acquired lock')
"""

    with locks.output_lock(path):
        child = subprocess.run(
            [sys.executable, "-c", script, str(path)], capture_output=True
        )

    assert child.returncode != 0
    assert b"BlockingIOError" in child.stderr


def test_contending_builds_are_rejected_and_killed_writer_releases_lock(tmp_path):
    """Real processes contend in three waves, then recover after a hard kill."""
    marker = tmp_path / "ready"
    env = {**os.environ, "PHYSICLAW_HW_OUTPUT": str(tmp_path)}
    holder_code = """
import sys
from pathlib import Path
from hardware.build_lock import build_lock
with build_lock():
    Path(sys.argv[1]).write_text('ready')
    sys.stdin.read()
"""
    contender_code = (
        "from hardware.build_lock import build_lock\n"
        "with build_lock(): print('acquired')"
    )

    def contend(_):
        return subprocess.run(
            [sys.executable, "-c", contender_code],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )

    with subprocess.Popen(
        [sys.executable, "-c", holder_code, str(marker)],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as holder:
        try:
            deadline = time.monotonic() + 15
            while not marker.exists():
                assert holder.poll() is None, holder.communicate(timeout=1)
                assert time.monotonic() < deadline, "lock holder did not start"
                time.sleep(0.01)
            with ThreadPoolExecutor(max_workers=8) as pool:
                for _ in range(3):
                    for result in pool.map(contend, range(8)):
                        assert result.returncode != 0
                        assert "already running" in result.stderr
                        assert "acquired" not in result.stdout
        finally:
            holder.kill()
            holder.communicate(timeout=5)

    result = contend(0)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "acquired"
