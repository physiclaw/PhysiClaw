"""Linux capture arbitration and read-only V4L2 focus metadata.

Native imports are lazy so other hosts can import the backend for unit tests.
"""

import importlib
import os
import struct
import threading
from collections import deque


class CaptureLock:
    """FIFO mutex: a pumping reader cannot overtake a queued control call.

    Context-manager acquisition is bounded; expired/interrupted waiters are
    removed before returning, so they can never execute a late camera command.
    Native calls already holding the mutex cannot be cancelled safely.
    """

    def __init__(self, *, timeout_error=TimeoutError, timeout: float = 2.0):
        self._condition = threading.Condition()
        self._waiting: deque[object] = deque()
        self._held = False
        self._timeout = timeout
        self._timeout_error = timeout_error

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        token = object()
        with self._condition:
            self._waiting.append(token)
            try:
                ready = self._condition.wait_for(
                    lambda: not self._held and self._waiting[0] is token,
                    timeout=0 if not blocking else (None if timeout < 0 else timeout),
                )
                if ready:
                    self._held = True
                return ready
            finally:
                self._waiting.remove(token)
                if not self._held:
                    self._condition.notify_all()

    def release(self) -> None:
        with self._condition:
            if not self._held:
                raise RuntimeError("release of unlocked capture lock")
            self._held = False
            self._condition.notify_all()

    def locked(self) -> bool:
        with self._condition:
            return self._held

    def __enter__(self):
        if not self.acquire(timeout=self._timeout):
            raise self._timeout_error(
                "Timed out waiting for camera capture/control access"
            )
        return self

    def __exit__(self, *_):
        self.release()


# Linux UAPI struct v4l2_queryctrl; fixed-width fields, no pointer-sized members.
_QUERYCTRL = struct.Struct("=II32siiiiIII")
_VIDIOC_QUERYCTRL = 0xC0000000 | (_QUERYCTRL.size << 16) | (ord("V") << 8) | 36
_FOCUS_ABSOLUTE = 0x009A090A


def focus_range(index: int) -> tuple[int, int, int] | None:
    """Query this camera's integer MF range without opening another stream.

    QUERYCTRL provides the bounds/step that OpenCV omits. Do not infer a range
    from one camera model. Missing/disabled/read-only controls are unavailable.
    See kernel userspace-api/media/v4l/vidioc-queryctrl.html.
    """
    if index < 0:
        return None  # OpenCV auto-selection does not identify an unambiguous node.
    try:
        fcntl = importlib.import_module("fcntl")
        fd = os.open(f"/dev/video{index}", os.O_RDONLY | getattr(os, "O_NONBLOCK"))
        try:
            data = bytearray(_QUERYCTRL.size)
            struct.pack_into("=I", data, 0, _FOCUS_ABSOLUTE)
            fcntl.ioctl(fd, _VIDIOC_QUERYCTRL, data)
        finally:
            os.close(fd)
    except (ImportError, OSError):
        return None
    _, kind, _, low, high, step, _, flags, _, _ = _QUERYCTRL.unpack(data)
    if kind != 1 or flags & (1 | 4) or low < 0 or high < low or step <= 0:
        return None
    return low, high, step
