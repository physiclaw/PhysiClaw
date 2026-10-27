"""Native inheritance windows close even when process setup fails."""

import os
from types import SimpleNamespace

import pytest

from hardware.port import locks
from hardware.port.backends import windows


@pytest.mark.skipif(os.name != "nt", reason="Native Windows byte-range locks")
@pytest.mark.parametrize("acquire", [locks.file_lock, locks.output_lock])
def test_regression_empty_locked_file_reports_contention(tmp_path, acquire):
    import msvcrt

    path = tmp_path / "output.lock"
    with path.open("w+b", buffering=0) as holder:
        msvcrt.locking(holder.fileno(), msvcrt.LK_NBLCK, 1)

        with pytest.raises(BlockingIOError):
            with acquire(path):
                pytest.fail("a second caller acquired the empty-file lock")

    assert path.read_bytes() == b""


def test_windows_startup_failure_restores_handle_inheritability(mocker, monkeypatch):
    native_os = SimpleNamespace(
        environ={"PHYSICLAW_HW_LOCK_HANDLE": "123"},
        get_handle_inheritable=mocker.Mock(return_value=False),
        set_handle_inheritable=mocker.Mock(),
    )
    monkeypatch.setattr(windows, "os", native_os)
    monkeypatch.setattr(
        windows,
        "subprocess",
        SimpleNamespace(STARTUPINFO=mocker.Mock(side_effect=OSError("startup setup"))),
    )

    with pytest.raises(OSError, match="startup setup"):
        with windows.child_inheritance():
            pytest.fail("startup failed before a child could be launched")

    assert native_os.set_handle_inheritable.call_args_list == [
        mocker.call(123, True),
        mocker.call(123, False),
    ]


@pytest.mark.skipif(os.name != "nt", reason="Native Windows handle recycling")
def test_recycled_pipe_handle_is_not_an_inherited_output_lock(tmp_path, monkeypatch):
    import msvcrt

    read_fd, write_fd = os.pipe()
    path = tmp_path / "output.lock"
    with os.fdopen(read_fd, "rb", buffering=0), os.fdopen(write_fd, "wb", buffering=0):
        monkeypatch.setenv(
            "PHYSICLAW_HW_LOCK_HANDLE", str(msvcrt.get_osfhandle(read_fd))
        )

        with locks.output_lock(path):
            acquired = True

    assert acquired and path.is_file()
