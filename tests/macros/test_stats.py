"""Tests for `physiclaw.macros.stats` — the single stats.json:
counter folding, consecutive-abort streaks, pruning, and fail-open IO."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from freezegun import freeze_time

from physiclaw.common import paths
from physiclaw.macros import stats
from physiclaw.port.locks import exclusive_file_lock


def _record_ok(name: str = "demo", known: set[str] | None = None) -> None:
    stats.record(name, ok=True, known_names=known if known is not None else {name})


def _record_abort(name: str = "demo", known: set[str] | None = None) -> None:
    stats.record(
        name,
        ok=False,
        known_names=known if known is not None else {name},
        step=2,
        reason="guard_failed",
        detail="'WeChat' not found",
    )


# ---------- recording ----------


@freeze_time("2026-08-02 10:11:00")
def test_record_success_writes_counters_and_timestamps() -> None:
    _record_ok()

    entry = stats.load()["demo"]

    assert entry["total_runs"] == 1
    assert entry["total_successes"] == 1
    assert entry["total_aborts"] == 0
    assert entry["consecutive_aborts"] == 0
    assert entry["last_run_at"] == "2026-08-02T10:11:00"
    assert entry["last_success_at"] == "2026-08-02T10:11:00"


@freeze_time("2026-08-02 10:11:00")
def test_record_abort_writes_last_abort_details() -> None:
    _record_abort()

    entry = stats.load()["demo"]

    assert entry["total_aborts"] == 1
    assert entry["consecutive_aborts"] == 1
    assert entry["last_abort"] == {
        "ts": "2026-08-02T10:11:00",
        "step": 2,
        "reason": "guard_failed",
        "detail": "'WeChat' not found",
        "run": None,
    }


def test_record_abort_carries_the_run_id() -> None:
    stats.record(
        "demo",
        ok=False,
        known_names={"demo"},
        step=1,
        reason="tool_error",
        detail="d",
        run_id="macro-run-a3f9c1",
    )

    assert stats.load()["demo"]["last_abort"]["run"] == "macro-run-a3f9c1"


def test_record_success_resets_consecutive_aborts() -> None:
    _record_abort()
    _record_abort()

    _record_ok()

    entry = stats.load()["demo"]
    assert entry["consecutive_aborts"] == 0
    assert entry["total_aborts"] == 2
    assert entry["total_runs"] == 3


def test_record_aborts_accumulate_streak() -> None:
    _record_abort()
    _record_abort()

    entry = stats.load()["demo"]

    assert entry["consecutive_aborts"] == 2


def test_record_prunes_keys_without_a_macro_dir() -> None:
    _record_ok("gone", known={"gone"})

    _record_ok("demo", known={"demo"})  # 'gone' no longer exists on disk

    assert set(stats.load()) == {"demo"}


def test_record_keeps_other_known_macros() -> None:
    _record_ok("a", known={"a", "b"})
    _record_ok("b", known={"a", "b"})

    assert set(stats.load()) == {"a", "b"}


# ---------- fail-open IO ----------


def test_load_missing_file_returns_empty() -> None:
    assert stats.load() == {}


def test_load_corrupt_file_returns_empty() -> None:
    paths.macros_dir().mkdir(parents=True)
    stats.stats_file().write_text("{not json", encoding="utf-8")

    assert stats.load() == {}


def test_load_non_dict_payload_returns_empty() -> None:
    paths.macros_dir().mkdir(parents=True)
    stats.stats_file().write_text("[1, 2]", encoding="utf-8")

    assert stats.load() == {}


def test_record_over_corrupt_file_starts_fresh() -> None:
    paths.macros_dir().mkdir(parents=True)
    stats.stats_file().write_text("{not json", encoding="utf-8")

    _record_ok()

    assert stats.load()["demo"]["total_runs"] == 1


def test_record_over_a_scalar_entry_resets_that_key_instead_of_raising() -> None:
    # A hand-edited or truncated file can leave a scalar under a macro key.
    # `record` runs AFTER the gestures physically fired, so raising here
    # would cost the agent its step log and its screen.
    paths.macros_dir().mkdir(parents=True)
    stats.stats_file().write_text('{"demo": 3}', encoding="utf-8")

    _record_ok()

    assert stats.load()["demo"]["total_runs"] == 1


@pytest.mark.parametrize(
    ("field", "ok"),
    [
        ("total_runs", True),
        ("total_successes", True),
        ("total_aborts", False),
        ("consecutive_aborts", False),
    ],
)
@pytest.mark.parametrize("corrupt", [None, "bad", [], {}, True, -2, 1.5])
def test_record_corrupt_counter_restarts_as_an_integer(
    field: str, ok: bool, corrupt: object
) -> None:
    paths.macros_dir().mkdir(parents=True)
    stats.stats_file().write_text(
        json.dumps({"demo": {field: corrupt, "last_success_at": "earlier"}}),
        encoding="utf-8",
    )

    stats.record("demo", ok=ok, known_names={"demo"})

    saved = json.loads(stats.stats_file().read_text(encoding="utf-8"))["demo"]
    assert saved[field] == 1
    assert type(saved[field]) is int


def test_load_sanitizes_counters_and_abort_shape_preserving_valid_history() -> None:
    paths.macros_dir().mkdir(parents=True)
    stats.stats_file().write_text(
        json.dumps(
            {
                "demo": {
                    "total_runs": 8,
                    "total_aborts": "bad",
                    "last_abort": "bad",
                    "last_success_at": "earlier",
                },
                "broken": 3,
            }
        ),
        encoding="utf-8",
    )

    loaded = stats.load()

    assert loaded["demo"]["total_runs"] == 8
    assert loaded["demo"]["total_aborts"] == 0
    assert loaded["demo"]["last_success_at"] == "earlier"
    assert loaded["demo"].get("last_abort") is None
    assert loaded["broken"]["total_runs"] == 0


def test_bad_input_does_not_move_the_re_rehearse_streak() -> None:
    # `consecutive_aborts` means "the app layout changed under this macro".
    # A bad_input run never reached the screen, so it must not say that —
    # three typos in a row would make a healthy macro read as decayed.
    for _ in range(3):
        stats.record(
            "demo",
            ok=False,
            known_names={"demo"},
            step=0,
            reason="bad_input",
            detail="missing required input 'msg'",
        )

    entry = stats.load()["demo"]

    assert entry["total_runs"] == 3
    assert entry["total_aborts"] == 3  # still a failed run
    assert entry["consecutive_aborts"] == 0  # but not a layout signal
    assert entry["last_abort"]["reason"] == "bad_input"


def test_bad_input_does_not_clear_a_real_streak_either() -> None:
    _record_abort()
    _record_abort()
    stats.record("demo", ok=False, known_names={"demo"}, reason="bad_input")

    assert stats.load()["demo"]["consecutive_aborts"] == 2


def test_record_write_failure_does_not_raise(mocker) -> None:
    mocker.patch(
        "physiclaw.macros.stats.write_json_atomic",
        side_effect=OSError("disk full"),
    )

    _record_ok()  # must not raise


def test_busy_stats_lock_skips_the_update_before_reading(monkeypatch, mocker) -> None:
    def no_wait(path, *, timeout):
        assert timeout == stats._LOCK_TIMEOUT_SECONDS
        return exclusive_file_lock(path)

    monkeypatch.setattr(stats, "exclusive_file_lock", no_wait)
    read = mocker.patch.object(stats, "load")
    write = mocker.patch.object(stats, "write_json_atomic")
    with exclusive_file_lock(stats.stats_file().with_suffix(".lock")):
        _record_ok()  # bounded diagnostic contention must not fail the run

    read.assert_not_called()
    write.assert_not_called()


def test_stats_lock_io_failure_does_not_fail_a_completed_run(mocker) -> None:
    mocker.patch.object(
        stats, "exclusive_file_lock", side_effect=PermissionError("read-only home")
    )
    read = mocker.patch.object(stats, "load")

    _record_ok()

    read.assert_not_called()


def test_concurrent_processes_preserve_every_run_counter(tmp_path) -> None:
    program = """
import sys, time
from pathlib import Path
from physiclaw.macros import stats
directory, worker = Path(sys.argv[1]), sys.argv[2]
original_load = stats.load
def slow_load():
    snapshot = original_load()
    # Widen the read/write race; the whole pause must stay inside the lock.
    time.sleep(0.01)
    return snapshot
stats.load = slow_load
(directory / ('ready-' + worker)).touch()
deadline = time.monotonic() + 10
while not (directory / 'start').exists():
    if time.monotonic() > deadline:
        raise TimeoutError('stats writer start barrier')
    time.sleep(0.005)
for _ in range(12):
    stats.record('shared', ok=worker == 'success', known_names={'shared'},
                 step=1, reason='guard_failed')
"""
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
    }
    processes = []
    try:
        for worker in ("success", "abort"):
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-c", program, str(tmp_path), worker],
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        deadline = time.monotonic() + 10
        while not all((tmp_path / f"ready-{w}").exists() for w in ("success", "abort")):
            assert time.monotonic() < deadline, (
                "stats writers did not reach the barrier"
            )
            time.sleep(0.005)
        (tmp_path / "start").touch()
        for process in processes:
            _, errors = process.communicate(timeout=10)
            assert process.returncode == 0, errors
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)

    saved = stats.load()["shared"]
    assert saved["total_runs"] == 24
    assert saved["total_successes"] == 12
    assert saved["total_aborts"] == 12


def test_stats_file_is_valid_json_on_disk() -> None:
    _record_ok()

    data = json.loads(stats.stats_file().read_text(encoding="utf-8"))

    assert "demo" in data
