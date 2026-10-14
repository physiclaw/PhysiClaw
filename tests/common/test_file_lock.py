"""Stable file locks serialize writers and release handles on every exit.

Mutation review accepts diagnostic wording and Windows lock-file initialization
variants: the lock covers byte zero regardless of initial length/content, since
Windows byte-range locks may extend past EOF. See Microsoft's locking reference:
https://learn.microsoft.com/en-us/cpp/c-runtime-library/reference/locking
The deadline equality variant is also accepted: a zero-duration retry before
the advancing monotonic clock exceeds the deadline does not extend the budget.
"""

import os
import sys
from types import SimpleNamespace

import pytest
from freezegun import freeze_time

from physiclaw.common import file_lock
from physiclaw.common.file_lock import FileLockBusy, exclusive_file_lock


def test_lock_creates_parents_and_rejects_competing_holder(tmp_path, mocker):
    path = tmp_path / "nested" / "deeper" / "operation.lock"
    sleep = mocker.patch.object(
        file_lock.time,
        "sleep",
        side_effect=AssertionError("default lock must not wait"),
    )

    with exclusive_file_lock(path):
        with pytest.raises(FileLockBusy, match="lock is held"):
            with exclusive_file_lock(path):
                pytest.fail("a second caller acquired the lock")

    assert path.is_file()
    sleep.assert_not_called()


def test_lock_is_released_after_body_raises(tmp_path):
    path = tmp_path / "operation.lock"

    with pytest.raises(ValueError, match="body failed"):
        with exclusive_file_lock(path):
            raise ValueError("body failed")

    with exclusive_file_lock(path):
        assert path.is_file()


@pytest.mark.parametrize("existing", [b"", b"\0", b"old contents"])
def test_windows_lock_covers_byte_zero_and_closes_handle(
    tmp_path, monkeypatch, existing
):
    path = tmp_path / "operation.lock"
    path.write_bytes(existing)
    calls = []

    def locking(fd, mode, size):
        calls.append((fd, mode, size, os.lseek(fd, 0, os.SEEK_CUR)))

    monkeypatch.setitem(sys.modules, "fcntl", None)
    monkeypatch.setitem(
        sys.modules, "msvcrt", SimpleNamespace(locking=locking, LK_NBLCK=2)
    )

    with exclusive_file_lock(path):
        pass

    assert len(calls) == 1
    assert calls[0][1:] == (2, 1, 0)
    with pytest.raises(OSError):
        os.fstat(calls[0][0])


def test_windows_contention_is_reported_before_entering_body(
    tmp_path, monkeypatch, mocker
):
    monkeypatch.setitem(sys.modules, "fcntl", None)
    monkeypatch.setitem(
        sys.modules,
        "msvcrt",
        SimpleNamespace(locking=mocker.Mock(side_effect=OSError("busy")), LK_NBLCK=2),
    )

    with pytest.raises(FileLockBusy, match="lock is held"):
        with exclusive_file_lock(tmp_path / "operation.lock"):
            pytest.fail("busy lock entered")


def test_lock_waits_until_competing_holder_releases(tmp_path, mocker):
    path = tmp_path / "operation.lock"
    holder = exclusive_file_lock(path)
    holder.__enter__()
    sleep = mocker.patch.object(
        file_lock.time, "sleep", side_effect=lambda _: holder.__exit__(None, None, None)
    )

    try:
        with exclusive_file_lock(path, timeout=1):
            acquired = True
    finally:
        holder.__exit__(None, None, None)

    assert acquired
    sleep.assert_called_once_with(0.01)


def test_lock_timeout_stops_waiting_without_entering_body(tmp_path, mocker):
    path = tmp_path / "operation.lock"

    with freeze_time("1970-01-01") as clock:
        sleep = mocker.patch.object(
            file_lock.time, "sleep", side_effect=lambda _: clock.tick(0.01)
        )
        with exclusive_file_lock(path):
            with pytest.raises(FileLockBusy, match="lock is held"):
                with exclusive_file_lock(path, timeout=0.025):
                    pytest.fail("busy lock entered")

    assert sleep.call_count == 3
    assert sum(call.args[0] for call in sleep.call_args_list) == pytest.approx(0.025)


def test_lock_does_not_retry_a_body_that_raises_busy(tmp_path, mocker):
    sleep = mocker.patch.object(file_lock.time, "sleep")
    body = mocker.Mock(side_effect=FileLockBusy("body failed"))

    with pytest.raises(FileLockBusy, match="body failed"):
        with exclusive_file_lock(tmp_path / "operation.lock", timeout=1):
            body()

    body.assert_called_once_with()
    sleep.assert_not_called()


@pytest.mark.parametrize("timeout", [-1, float("inf"), float("nan")])
def test_lock_rejects_invalid_timeout(tmp_path, timeout):
    path = tmp_path / "operation.lock"

    with pytest.raises(ValueError, match="timeout must be finite and nonnegative"):
        with exclusive_file_lock(path, timeout=timeout):
            pytest.fail("invalid timeout entered")

    assert not path.exists()
