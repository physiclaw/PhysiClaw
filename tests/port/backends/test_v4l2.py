"""Linux metadata and capture arbitration; no physical device required."""

import threading
import time
from types import SimpleNamespace

import pytest

from physiclaw.port.backends import darwin, linux, v4l2, windows


@pytest.fixture(autouse=True)
def isolated_native_os(monkeypatch, mocker):
    """Model the Linux syscall boundary even on hosts without O_NONBLOCK.

    Replace this module's OS reference, not the shared os module. Unexpected
    native opens fail the test instead of touching a connected camera.
    """
    monkeypatch.setattr(
        v4l2,
        "os",
        SimpleNamespace(
            O_RDONLY=0,
            O_NONBLOCK=0x800,
            open=mocker.Mock(side_effect=AssertionError("unexpected device open")),
            close=mocker.Mock(),
        ),
    )


@pytest.fixture
def metadata(mocker):
    v4l2.os.open.side_effect = None
    v4l2.os.open.return_value = 123
    ioctl = mocker.Mock()
    mocker.patch.object(
        v4l2.importlib, "import_module", return_value=SimpleNamespace(ioctl=ioctl)
    )
    return ioctl


def wait_queued(lock, count):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with lock._condition:
            if len(lock._waiting) == count:
                return
        time.sleep(0.001)
    pytest.fail("worker did not queue for the capture lock")


def test_reader_cannot_reacquire_ahead_of_waiting_controls():
    # Queue ordering, not OS scheduler speed, is the contract under test.
    lock = v4l2.CaptureLock(timeout=30)
    order = []
    threads = []
    lock.acquire()

    def worker(number):
        with lock:
            order.append(number)

    try:
        for number in range(5):
            thread = threading.Thread(target=worker, args=(number,), daemon=True)
            threads.append(thread)
            thread.start()
            wait_queued(lock, number + 1)
    finally:
        lock.release()
    # This models the reader immediately starting its next read. Plain Lock
    # allows it to overtake waiters; FIFO must put it behind all five controls.
    with lock:
        order.append("reader")
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert order == [0, 1, 2, 3, 4, "reader"]


def test_expired_control_never_runs_later_or_blocks_the_queue():
    lock = v4l2.CaptureLock(timeout=0.01)
    executed = []
    errors = []
    lock.acquire()

    def expired():
        try:
            with lock:
                executed.append(True)
        except TimeoutError as exc:
            errors.append(exc)

    thread = threading.Thread(target=expired, daemon=True)
    thread.start()
    thread.join(timeout=5)
    lock.release()
    assert not thread.is_alive()
    assert len(errors) == 1
    with lock:
        assert not executed
        assert not lock._waiting


def test_interrupted_waiter_is_removed(mocker):
    lock = v4l2.CaptureLock()
    lock.acquire()
    patched = mocker.patch.object(
        lock._condition, "wait_for", side_effect=KeyboardInterrupt
    )
    with pytest.raises(KeyboardInterrupt):
        lock.acquire()
    mocker.stop(patched)
    assert not lock._waiting
    lock.release()
    assert lock.acquire(blocking=False)
    lock.release()


def test_linux_context_timeout_uses_callers_error_type():
    class CameraTimeout(RuntimeError):
        pass

    lock = v4l2.CaptureLock(timeout_error=CameraTimeout, timeout=0)
    lock.acquire()
    try:
        assert not lock.acquire(timeout=0)
        with pytest.raises(CameraTimeout, match="camera"):
            with lock:
                pytest.fail("timed-out operation executed")
    finally:
        lock.release()
    assert not lock.locked()
    with pytest.raises(RuntimeError, match="unlocked"):
        lock.release()


@pytest.mark.parametrize("backend", [windows, darwin])
def test_other_platforms_keep_plain_lock_and_existing_calibration(backend, mocker):
    probe = mocker.patch.object(v4l2, "focus_range", side_effect=AssertionError)
    assert type(backend.camera_capture_lock()) is type(threading.Lock())
    assert backend.camera_focus_search_range(0) is None
    probe.assert_not_called()


def test_linux_selects_fair_lock_and_native_metadata(mocker):
    probe = mocker.patch.object(v4l2, "focus_range", return_value=(0, 1023, 1))
    assert isinstance(linux.camera_capture_lock(), v4l2.CaptureLock)
    assert linux.camera_focus_search_range(7) == (0, 1023, 1)
    probe.assert_called_once_with(7)


@pytest.mark.parametrize("low,high,step", [(0, 1023, 1), (10, 110, 5), (0, 0, 1)])
def test_focus_range_reads_kernel_bounds_and_closes_fd(metadata, low, high, step):
    def ioctl(fd, request, data):
        assert fd == 123 and request == 0xC0445624
        assert v4l2._QUERYCTRL.unpack(data)[0] == 0x009A090A
        data[:] = v4l2._QUERYCTRL.pack(
            0x009A090A, 1, b"focus_absolute", low, high, step, low, 0x10, 0, 0
        )

    metadata.side_effect = ioctl
    assert v4l2.focus_range(7) == (low, high, step)
    assert v4l2.os.open.call_args.args[0] == "/dev/video7"
    v4l2.os.close.assert_called_once_with(123)


@pytest.mark.parametrize(
    "flags,kind,low,high,step",
    [
        (1, 1, 0, 10, 1),
        (4, 1, 0, 10, 1),
        (0, 2, 0, 10, 1),
        (0, 1, -1, 10, 1),
        (0, 1, 20, 10, 1),
        (0, 1, 0, 10, 0),
    ],
)
def test_unusable_focus_metadata_is_not_a_search_range(
    metadata, flags, kind, low, high, step
):
    def ioctl(fd, request, data):
        data[:] = v4l2._QUERYCTRL.pack(
            0x009A090A, kind, b"", low, high, step, 0, flags, 0, 0
        )

    metadata.side_effect = ioctl
    assert v4l2.focus_range(0) is None
    v4l2.os.close.assert_called_once_with(123)


def test_missing_control_closes_metadata_fd(metadata):
    metadata.side_effect = OSError("unsupported")
    assert v4l2.focus_range(0) is None
    v4l2.os.close.assert_called_once_with(123)


def test_negative_index_does_not_probe_an_arbitrary_device():
    assert v4l2.focus_range(-1) is None
    v4l2.os.open.assert_not_called()


@pytest.mark.parametrize("error", [FileNotFoundError, PermissionError])
def test_absent_or_inaccessible_camera_has_no_focus_range(metadata, error):
    v4l2.os.open.side_effect = error("camera unavailable")
    assert v4l2.focus_range(0) is None
    v4l2.os.close.assert_not_called()


def test_missing_fcntl_does_not_open_a_device(mocker):
    mocker.patch.object(
        v4l2.importlib, "import_module", side_effect=ModuleNotFoundError
    )
    assert v4l2.focus_range(0) is None
    v4l2.os.open.assert_not_called()
