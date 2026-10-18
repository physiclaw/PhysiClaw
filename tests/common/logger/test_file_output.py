"""Portable file-sink contracts; runnable with pytest alone and --noconftest."""

import datetime as dt
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from physiclaw.common import config
from physiclaw.common.file_lock import exclusive_file_lock
from physiclaw.common.logger import logger as logger_mod
from physiclaw.common.logger.retention import purge_daily_logs

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def sink(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG", config.Config())
    config.CONFIG.retention.log_max_bytes = 20
    config.CONFIG.retention.log_backup_count = 2
    handler = logger_mod._DailyFileHandler(tmp_path, "runtime", "runtime")
    handler.setFormatter(logging.Formatter("%(message)s"))
    yield handler
    handler.close()


def emit(sink, text):
    sink.handle(
        logging.LogRecord("physiclaw.test", logging.INFO, __file__, 1, text, (), None)
    )


def test_size_rotation_counts_utf8_bytes_and_bounds_archives(sink):
    path = Path(sink.baseFilename)
    # Six code points but 16 bytes per record, including its LF.
    for i in range(4):
        emit(sink, f"{i}中文测试啊")
        assert sink.stream is None  # Peers can rename on Windows.

    assert path.read_bytes() == "3中文测试啊\n".encode()
    assert Path(f"{path}.1").read_bytes() == "2中文测试啊\n".encode()
    assert Path(f"{path}.2").read_bytes() == "1中文测试啊\n".encode()
    assert not Path(f"{path}.3").exists()


def test_large_single_record_is_preserved_then_rotated(sink):
    path = Path(sink.baseFilename)
    large = "long traceback\n" * 50
    emit(sink, large)
    emit(sink, "next event")

    assert Path(f"{path}.1").read_text(encoding="utf-8") == large + "\n"
    assert path.read_bytes() == b"next event\n"


def test_surrogate_and_newlines_have_stable_disk_encoding(sink):
    emit(sink, "test\ud800\nnext")
    assert Path(sink.baseFilename).read_bytes() == b"test\\ud800\nnext\n"


def test_retention_expires_numbered_archives_and_leaves_other_files(tmp_path):
    stale = [
        "runtime-2000-01-01.log",
        "runtime-2000-01-01.log.1",
        "runtime-2000-01-01.log.2",
    ]
    untouched = [
        "runtime-2000-01-01.log.tmp",
        "runtime-2000-01-01.log.backup",
        "other-2000-01-01.log",
        ".runtime.log.lock",
    ]
    fresh = ["runtime-2026-10-18.log", "runtime-2026-10-18.log.1"]
    for name in stale + untouched + fresh:
        path = tmp_path / name
        path.write_text("evidence", encoding="utf-8")
        if name not in fresh:
            os.utime(path, (1, 1))

    purge_daily_logs(tmp_path, "runtime", days=30)

    assert all(not (tmp_path / name).exists() for name in stale)
    assert all((tmp_path / name).exists() for name in untouched + fresh)


def test_long_running_handler_purges_at_date_change(sink, monkeypatch):
    path = Path(sink.baseFilename)
    old = path.parent / "runtime-2000-01-01.log.1"
    old.write_text("expired", encoding="utf-8")
    os.utime(old, (1, 1))
    tomorrow = dt.datetime.now() + dt.timedelta(days=1)

    class NextDay(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return tomorrow

    monkeypatch.setattr(logger_mod.dt, "datetime", NextDay)
    emit(sink, "after midnight")

    assert not old.exists()
    assert Path(sink.baseFilename).name == f"runtime-{tomorrow:%Y-%m-%d}.log"
    assert Path(sink.baseFilename).read_bytes() == b"after midnight\n"


def test_rotation_failure_preserves_records_and_warns_once(sink, monkeypatch):
    # Route the actual warning back through the same file handler: this must
    # neither recurse nor deadlock, even when every rollover fails.
    logger = logger_mod.log
    monkeypatch.setattr(logger, "handlers", [sink])
    monkeypatch.setattr(logger, "propagate", False)
    monkeypatch.setattr(logger, "level", logging.DEBUG)

    def deny_rotation(*args):
        raise PermissionError("rename denied")

    monkeypatch.setattr(sink, "rotate", deny_rotation)
    emit(sink, "initial event")
    emit(sink, "second event")
    emit(sink, "third event")

    text = Path(sink.baseFilename).read_text(encoding="utf-8")
    assert all(
        event in text for event in ("initial event", "second event", "third event")
    )
    assert text.count("Cannot rotate") == 1
    assert "PermissionError: rename denied" in text
    assert sink.stream is None


def test_handler_generated_logging_runs_without_holding_its_lock(sink, monkeypatch):
    acquired = []

    def report(*args, **kwargs):
        def try_lock():
            locked = sink.lock.acquire(blocking=False)
            acquired.append(locked)
            if locked:
                sink.lock.release()

        thread = threading.Thread(target=try_lock)
        thread.start()
        thread.join(timeout=1)
        assert not thread.is_alive()

    def deny_rotation(*args):
        raise PermissionError("rename denied")

    monkeypatch.setattr(logger_mod.log, "warning", report)
    monkeypatch.setattr(sink, "rotate", deny_rotation)
    emit(sink, "initial event")
    emit(sink, "second event")
    assert acquired == [True]


def test_failed_rotation_notice_cannot_fail_a_completed_action(sink, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("sink unavailable")

    errors = []
    monkeypatch.setattr(sink, "handleError", errors.append)
    monkeypatch.setattr(sink, "rotate", fail)
    monkeypatch.setattr(logger_mod.log, "warning", fail)
    emit(sink, "initial event")
    emit(sink, "completed action")

    assert len(errors) == 1
    assert "completed action" in Path(sink.baseFilename).read_text(encoding="utf-8")


def test_busy_sink_does_not_raise_into_the_caller(sink, monkeypatch):
    errors = []
    monkeypatch.setattr(sink, "handleError", errors.append)

    # No waiting is necessary to exercise the handler's fail-open path.
    def busy(path, *, timeout):
        return exclusive_file_lock(path)

    monkeypatch.setattr(logger_mod, "exclusive_file_lock", busy)
    with exclusive_file_lock(sink._lock_path):
        emit(sink, "completed action")

    assert len(errors) == 1
    assert errors[0].getMessage() == "completed action"
    assert sink.stream is None


def test_concurrent_processes_rotate_without_losing_or_duplicating_records(tmp_path):
    program = """
import logging, sys, time
from pathlib import Path
from physiclaw.common.config import CONFIG
from physiclaw.common.logger.logger import _DailyFileHandler
directory, worker = Path(sys.argv[1]), sys.argv[2]
CONFIG.retention.log_max_bytes = 128
CONFIG.retention.log_backup_count = 128
handler = _DailyFileHandler(directory, 'runtime', 'runtime')
handler.setFormatter(logging.Formatter('%(message)s'))
(directory / ('ready-' + worker)).touch()
deadline = time.monotonic() + 10
while not (directory / 'start').exists():
    if time.monotonic() > deadline:
        raise TimeoutError('writer start barrier')
    time.sleep(0.01)
for i in range(60):
    handler.handle(logging.LogRecord('physiclaw.test', logging.INFO, '', 0, f'{worker}:{i}:中文', (), None))
handler.close()
"""
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "src"),
        "PHYSICLAW_HOME": str(tmp_path / "home"),
    }
    processes = []
    try:
        for worker in range(3):
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-S", "-c", program, str(tmp_path), str(worker)],
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            )
        deadline = time.monotonic() + 10
        while len(list(tmp_path.glob("ready-*"))) < 3:
            assert time.monotonic() < deadline, "writers failed to reach start barrier"
            time.sleep(0.01)
        (tmp_path / "start").touch()
        for process in processes:
            stdout, stderr = process.communicate(timeout=15)
            assert process.returncode == 0, stderr.decode(errors="replace")
            assert not stdout and not stderr
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()

    lines = []
    for path in tmp_path.glob("runtime-*.log*"):
        assert path.stat().st_size <= 128
        lines.extend(path.read_text(encoding="utf-8").splitlines())
    assert len(lines) == 180
    assert set(lines) == {
        f"{worker}:{i}:中文" for worker in range(3) for i in range(60)
    }
