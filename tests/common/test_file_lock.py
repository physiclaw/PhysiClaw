"""Stable file locks serialize writers and release handles on every exit.

Mutation review accepts diagnostic wording and Windows lock-file initialization
variants: the lock covers byte zero regardless of initial length/content, since
Windows byte-range locks may extend past EOF. See Microsoft's locking reference:
https://learn.microsoft.com/en-us/cpp/c-runtime-library/reference/locking
"""

import os
import sys
from types import SimpleNamespace

import pytest

from physiclaw.common.file_lock import FileLockBusy, exclusive_file_lock


def test_lock_creates_parents_and_rejects_competing_holder(tmp_path):
    path = tmp_path / "nested" / "deeper" / "operation.lock"

    with exclusive_file_lock(path):
        with pytest.raises(FileLockBusy, match="lock is held"):
            with exclusive_file_lock(path):
                pytest.fail("a second caller acquired the lock")

    assert path.is_file()


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
