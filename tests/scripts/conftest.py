"""Serial diagnostic doubles: deterministic time and no physical port access."""

from collections import deque
from types import SimpleNamespace

import pytest

from scripts import grbl_jog, grbl_solenoid_test


@pytest.fixture(params=[grbl_jog, grbl_solenoid_test], ids=["jog", "solenoid"])
def diagnostic_script(request):
    return request.param


class DiagnosticClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, duration):
        self.now += duration


class DiagnosticSerial:
    def __init__(self, clock, replies=(), *, byte_delay=0.0, responder=None):
        self.clock = clock
        self.replies = deque(replies)
        self.byte_delay = byte_delay
        self.responder = responder
        self.writes = []
        self.read_timeouts = []
        self.read_sizes = []
        self._timeout = 3.0
        self.timeout_updates = []
        self.closed = False

    @property
    def timeout(self):
        return self._timeout

    @timeout.setter
    def timeout(self, value):
        self.timeout_updates.append(value)
        self._timeout = value

    @property
    def in_waiting(self):
        return len(self.replies[0]) if self.replies else 0

    def read(self, size):
        self.read_timeouts.append(self.timeout)
        self.read_sizes.append(size)
        if not self.replies:
            self.clock.sleep(self.timeout)
            return b""
        self.clock.sleep(min(self.byte_delay, self.timeout))
        chunk = self.replies.popleft()
        if not chunk:
            self.clock.sleep(self.timeout)
        if len(chunk) > size:
            self.replies.appendleft(chunk[size:])
        return chunk[:size]

    def write(self, data):
        self.writes.append(data)
        if self.responder is not None:
            self.replies.extend(self.responder(data))
        return len(data)

    def reset_input_buffer(self):
        self.replies.clear()

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


@pytest.fixture
def diagnostic_device(monkeypatch):
    def make(module, replies=(), **kwargs):
        clock = DiagnosticClock()
        timing = SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep)
        monkeypatch.setattr(module, "time", timing)
        monkeypatch.setattr(module.grbl_protocol, "time", timing)
        return DiagnosticSerial(clock, replies, **kwargs)

    return make
