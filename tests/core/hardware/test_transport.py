"""Tests for `physiclaw.core.hardware.transport` — SerialTransport.

Protocol behavior (ok/error/alarm/optional) is exercised end-to-end
through the arm in `test_arm.py`; this file pins what the transport
adds: the typed error taxonomy and the port lock.
"""

from __future__ import annotations

from collections import deque
from types import SimpleNamespace

import pytest

from physiclaw.core.hardware import transport as transport_mod
from physiclaw.core.hardware.device import (
    DeviceAlarm,
    DeviceNotFound,
    DeviceTimeout,
    HardwareError,
    ProtocolError,
)
from physiclaw.core.hardware.transport import SerialTransport


class _FakeSerial:
    def __init__(self, responses=()):
        self.writes: list[bytes] = []
        self.responses = deque(responses)
        self.timeout = 3.0
        self.closed = False

    @property
    def in_waiting(self):
        return len(self.responses[0]) if self.responses else 0

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        return len(data)

    def read(self, n: int) -> bytes:
        chunk = self.responses.popleft() if self.responses else b""
        if len(chunk) > n:
            self.responses.appendleft(chunk[n:])
        return chunk[:n]

    def reset_input_buffer(self) -> None:
        self.responses.clear()

    def close(self) -> None:
        self.closed = True


def test_send_raises_typed_timeout() -> None:
    t = SerialTransport(_FakeSerial(responses=[b""] * 10))

    with pytest.raises(DeviceTimeout, match="GRBL not responding"):
        t.send("G0 X0 Y0")


def test_send_raises_typed_protocol_error() -> None:
    t = SerialTransport(_FakeSerial(responses=[b"error:23\n"]))

    with pytest.raises(ProtocolError, match="GRBL error"):
        t.send("G0 X0 Y0")


def test_send_raises_typed_alarm() -> None:
    t = SerialTransport(_FakeSerial(responses=[b"ALARM:1\n"]))

    with pytest.raises(DeviceAlarm, match="GRBL alarm"):
        t.send("G0 X0 Y0")


def test_regression_unsolicited_alarm_cannot_shift_later_acknowledgments() -> None:
    ser = _FakeSerial([b"ALARM:1\nerror:9\nok\nerror:23\n"])
    transport = SerialTransport(ser)

    with pytest.raises(DeviceAlarm, match="reconnect before unlocking"):
        transport.send("G0 X1")
    with pytest.raises(ProtocolError, match="unsynchronized; reconnect"):
        transport.send("$X")
    with pytest.raises(ProtocolError, match="unsynchronized; reconnect"):
        transport.send("G0 X2")

    assert ser.writes == [b"G0 X1\n"]


def test_regression_diagnostics_cannot_extend_ack_deadline(mocker) -> None:
    ser = _FakeSerial([b"[MSG:waiting]\n"] * 10 + [b"ok\n"])
    clock = SimpleNamespace(now=0.0)
    mocker.patch.object(transport_mod.time, "monotonic", side_effect=lambda: clock.now)
    read_bytes = ser.read

    def timed_read(n):
        clock.now += 1.0
        return read_bytes(n)

    read = mocker.patch.object(ser, "read", side_effect=timed_read)
    transport = SerialTransport(ser)
    mocker.patch.object(transport, "RESPONSE_TIMEOUT_SECONDS", 3.0)

    with pytest.raises(DeviceTimeout, match="GRBL not responding"):
        transport.send("G4 P0.1")

    assert read.call_count == 3


def test_regression_split_ack_is_reassembled() -> None:
    ser = _FakeSerial([b"o", b"k\n"])
    transport = SerialTransport(ser)

    transport.send("G90")

    assert ser.writes == [b"G90\n"]


def test_regression_incomplete_ack_is_not_success() -> None:
    transport = SerialTransport(_FakeSerial([b"ok"]))

    with pytest.raises(DeviceTimeout, match="GRBL not responding"):
        transport.send("G90")


@pytest.mark.parametrize("reply", [b"o\xffk\n", b"error:\xff\n", b"ALARM:\xff\n"])
def test_regression_corrupt_reply_cannot_acknowledge_a_command(reply) -> None:
    ser = _FakeSerial([reply + b"ok\n"])
    transport = SerialTransport(ser)

    with pytest.raises(ProtocolError, match="Invalid UTF-8"):
        transport.send("G90")
    with pytest.raises(ProtocolError, match="unsynchronized; reconnect"):
        transport.send("G0 X1 Y1")

    assert ser.writes == [b"G90\n"]


@pytest.mark.parametrize("reply", [b"error:\n", b"error:nope\n", b"ALARM:x\n"])
def test_regression_malformed_terminator_does_not_restore_synchronization(
    reply,
) -> None:
    ser = _FakeSerial([reply + b"ok\n"])
    transport = SerialTransport(ser)

    with pytest.raises(ProtocolError, match="Malformed GRBL reply"):
        transport.send("G90")
    with pytest.raises(ProtocolError, match="unsynchronized; reconnect"):
        transport.send("G0 X1 Y1")

    assert ser.writes == [b"G90\n"]


def test_regression_unterminated_oversized_reply_is_rejected() -> None:
    ser = _FakeSerial([b"x" * 4096])
    transport = SerialTransport(ser)

    with pytest.raises(ProtocolError, match="reply exceeds"):
        transport.send("G90")


def test_regression_oversized_line_tail_cannot_ack_another_command() -> None:
    ser = _FakeSerial([b"x" * 4096 + b"ok\nerror:23\n"])
    transport = SerialTransport(ser)

    with pytest.raises(ProtocolError, match="reply exceeds"):
        transport.send("G90")
    with pytest.raises(ProtocolError, match="unsynchronized; reconnect"):
        transport.send("G0 X1 Y1")

    assert ser.writes == [b"G90\n"]


def test_regression_unacknowledged_command_still_allows_emergency_coil_off() -> None:
    ser = _FakeSerial()
    transport = SerialTransport(ser)

    with pytest.raises(DeviceTimeout, match="GRBL not responding"):
        transport.send("M3 S1000")
    with pytest.raises(ProtocolError, match="unsynchronized; reconnect"):
        transport.send("M5")

    assert ser.writes == [b"M3 S1000\n", b"M5\n"]


def test_regression_late_ack_does_not_count_as_success(mocker) -> None:
    ser = _FakeSerial([b"ok\n"])
    clock = SimpleNamespace(now=0.0)
    mocker.patch.object(transport_mod.time, "monotonic", side_effect=lambda: clock.now)

    def late_read(n):
        clock.now = 13.0
        return b"ok\n"

    mocker.patch.object(ser, "read", side_effect=late_read)
    transport = SerialTransport(ser)

    with pytest.raises(DeviceTimeout, match="GRBL not responding"):
        transport.send("G90")

    assert ser.timeout == 3.0


def test_native_read_uses_remaining_budget_and_restores_port_timeout(mocker) -> None:
    ser = _FakeSerial([b"ok\n"])
    clock = SimpleNamespace(now=0.0)
    observed = []
    mocker.patch.object(transport_mod.time, "monotonic", side_effect=lambda: clock.now)

    def read(n):
        observed.append(ser.timeout)
        clock.now += 0.1
        return b"ok\n"

    mocker.patch.object(ser, "read", side_effect=read)
    transport = SerialTransport(ser)
    transport.RESPONSE_TIMEOUT_SECONDS = 0.5

    transport.send("G90")

    assert observed == [0.5]
    assert ser.timeout == 3.0


def test_native_read_failure_restores_port_timeout(mocker) -> None:
    ser = _FakeSerial()
    mocker.patch.object(ser, "read", side_effect=OSError("unplugged"))
    transport = SerialTransport(ser)
    transport.RESPONSE_TIMEOUT_SECONDS = 0.5

    with pytest.raises(OSError, match="unplugged"):
        transport.send("G90")

    assert ser.timeout == 3.0


def test_query_status_skips_corrupted_coordinate_report(mocker) -> None:
    ser = _FakeSerial([b"<Idle|WPos:1.0,2\xff.5>\n"])
    mocker.patch.object(transport_mod.time, "sleep")

    status = SerialTransport(ser).query_status()

    assert status == ""


@pytest.mark.parametrize("reply", [b"<Idle", b"<Idle|WPos:1.0,2"])
def test_regression_query_status_rejects_incomplete_report(reply, mocker) -> None:
    ser = _FakeSerial([reply])
    mocker.patch.object(transport_mod.time, "sleep")

    status = SerialTransport(ser).query_status()

    assert status == ""


@pytest.mark.parametrize("diagnostic", [b"[MSG:ready]", b"[MSG:ready -> waiting]"])
def test_query_status_accepts_report_after_diagnostic(diagnostic, mocker) -> None:
    reply = diagnostic + b"\r\n<Idle|WPos:1.0,2.0>\r\n"
    ser = _FakeSerial([reply])
    mocker.patch.object(transport_mod.time, "sleep")

    status = SerialTransport(ser).query_status()

    assert status == "<Idle|WPos:1.0,2.0>"


def test_hierarchy_is_runtime_error() -> None:
    """Pre-existing `except RuntimeError` handlers up the stack must keep
    catching driver failures — the taxonomy refines, never escapes."""
    for exc in (DeviceNotFound, DeviceTimeout, ProtocolError, DeviceAlarm):
        assert issubclass(exc, HardwareError)
        assert issubclass(exc, RuntimeError)


def test_send_serializes_against_concurrent_sender() -> None:
    """Two threads interleaving writes would desync GRBL's one-reply-per-
    command stream — the transport lock forces one wire interaction at a
    time."""
    import threading

    order: list[str] = []

    class _SlowSerial(_FakeSerial):
        def write(self, data: bytes) -> int:
            order.append(f"write:{data!r}")
            return len(data)

        def read(self, n) -> bytes:
            # First reader parks until the main thread releases it.
            evt = events.popleft() if events else None
            if evt is not None:
                evt.wait(timeout=2.0)
            order.append("read")
            return super().read(n)

    events: deque[threading.Event] = deque()
    gate = threading.Event()
    events.append(gate)
    t = SerialTransport(_SlowSerial([b"ok\n", b"ok\n"]))

    first = threading.Thread(target=lambda: t.send("G0 X1 Y1"))
    first.start()
    # Give the first send time to enter the locked region and park.
    for _ in range(100):
        if order and order[0].startswith("write"):
            break
        first.join(timeout=0.01)

    second = threading.Thread(target=lambda: t.send("G0 X2 Y2"))
    second.start()
    second.join(timeout=0.05)
    assert second.is_alive()  # blocked on the transport lock
    assert len([o for o in order if o.startswith("write")]) == 1

    gate.set()
    first.join(timeout=2.0)
    second.join(timeout=2.0)
    assert not second.is_alive()
    # Full serialization: write/read of the first completes before the
    # second's write hits the wire.
    assert order == ["write:b'G0 X1 Y1\\n'", "read", "write:b'G0 X2 Y2\\n'", "read"]


def test_close_closes_port() -> None:
    fake = _FakeSerial()
    t = SerialTransport(fake)

    t.close()

    assert fake.closed is True


# ---------- send_bounded (teardown path) ----------


def test_send_bounded_sends_when_wire_is_free() -> None:
    ser = _FakeSerial(responses=[b"ok\n"])
    t = SerialTransport(ser)

    assert t.send_bounded("M5", lock_timeout=0.05) is True
    assert b"M5\n" in ser.writes


def test_send_bounded_skips_when_wire_lock_is_held() -> None:
    # An abandoned holder (daemon killed mid-read at interpreter exit)
    # must not hang an atexit teardown — skip the command; the port
    # close is the backstop.
    import threading

    ser = _FakeSerial(responses=[b"ok\n"])
    t = SerialTransport(ser)
    held = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with t._lock:
            held.set()
            release.wait(timeout=5)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    held.wait(timeout=5)
    try:
        assert t.send_bounded("M5", lock_timeout=0.05) is False
        assert b"M5\n" not in ser.writes
    finally:
        release.set()
        holder.join(timeout=5)
