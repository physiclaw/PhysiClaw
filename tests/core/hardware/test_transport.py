"""Tests for `physiclaw.core.hardware.transport` — SerialTransport.

The public transport API is exercised with fragmented serial replies, bounded
clocks and I/O faults. Assertions protect acknowledgment ownership, motion
refusal, emergency release, framing and lock behavior. `test_arm.py` also
exercises the protocol through physical gesture commands.
"""

from __future__ import annotations

from collections import deque
from itertools import count, pairwise
from types import SimpleNamespace

import pytest
from hypothesis import given
from hypothesis import strategies as st

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


@pytest.mark.parametrize(
    "command",
    [
        "",
        " ",
        "G0 X1\nG0 X2",
        "G0 X1\r",
        "G0\x00",
        "G0\x18",
        "G0?",
        "G0!",
        "G0~",
        "G0\x7f",
        "G0 é",
        None,
    ],
)
def test_invalid_command_never_writes_or_poison_acknowledgments(command):
    serial = _FakeSerial([b"ok\n"])
    transport = SerialTransport(serial)

    with pytest.raises(ValueError, match="one ASCII line"):
        transport.send(command)
    assert serial.writes == []

    transport.send("G90")
    assert serial.writes == [b"G90\n"]


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


@pytest.mark.parametrize("arrival", [12.0, 13.0])
def test_regression_late_ack_does_not_count_as_success(mocker, arrival) -> None:
    ser = _FakeSerial([b"ok\n"])
    clock = SimpleNamespace(now=0.0)
    mocker.patch.object(transport_mod.time, "monotonic", side_effect=lambda: clock.now)

    def late_read(n):
        clock.now = arrival
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
    from concurrent.futures import ThreadPoolExecutor

    from tests.timing_helpers import WAIT_TIMEOUT, ObservedLock

    order: list[str] = []
    reading = threading.Event()

    class _SlowSerial(_FakeSerial):
        def write(self, data: bytes) -> int:
            order.append(f"write:{data!r}")
            return len(data)

        def read(self, n) -> bytes:
            # First reader parks until the main thread releases it.
            evt = events.popleft() if events else None
            if evt is not None:
                reading.set()
                assert evt.wait(timeout=WAIT_TIMEOUT)
            order.append("read")
            return super().read(n)

    events: deque[threading.Event] = deque()
    gate = threading.Event()
    events.append(gate)
    t = SerialTransport(_SlowSerial([b"ok\n", b"ok\n"]))
    t._lock = ObservedLock(t._lock)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(t.send, "G0 X1 Y1")
        try:
            assert reading.wait(WAIT_TIMEOUT)
            second = pool.submit(t.send, "G0 X2 Y2")
            assert t._lock.contended.wait(WAIT_TIMEOUT)
            assert not second.done()
            assert len([o for o in order if o.startswith("write")]) == 1
        finally:
            gate.set()
        first.result(timeout=WAIT_TIMEOUT)
        second.result(timeout=WAIT_TIMEOUT)
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

    from tests.timing_helpers import WAIT_TIMEOUT

    ser = _FakeSerial(responses=[b"ok\n"])
    t = SerialTransport(ser)
    held = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with t._lock:
            held.set()
            assert release.wait(timeout=WAIT_TIMEOUT)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    try:
        assert held.wait(timeout=WAIT_TIMEOUT)
        assert t.send_bounded("M5", lock_timeout=0.05) is False
        assert b"M5\n" not in ser.writes
    finally:
        release.set()
        holder.join(timeout=WAIT_TIMEOUT)
    assert not holder.is_alive()


@pytest.mark.parametrize(
    "reply", [b"error:3\n", b"error: 3\n", b"error:162\n", b"error: 162\n"]
)
def test_setting_rejection_is_an_error_unless_explicitly_optional(reply):
    ser = _FakeSerial([reply + b"ok\n"])
    transport = SerialTransport(ser)

    with pytest.raises(ProtocolError, match="GRBL error"):
        transport.send("$32=0")
    transport.send("M5")

    assert ser.writes == [b"$32=0\n", b"M5\n"]


@pytest.mark.parametrize(
    "reply", [b"error:3\n", b"error: 3\n", b"error:162\n", b"error: 162\n"]
)
def test_optional_setting_rejection_leaves_next_command_acknowledgment_intact(reply):
    ser = _FakeSerial([reply + b"[MSG:ERR: read-only setting]\nok\nerror:23\n"])
    transport = SerialTransport(ser)

    transport.send("$32=0", optional=True)
    transport.send("M5")
    with pytest.raises(ProtocolError, match="error:23"):
        transport.send("G0 X1")

    assert ser.writes == [b"$32=0\n", b"M5\n", b"G0 X1\n"]


@pytest.mark.parametrize("reply", [b"error:2\n", b"error:23\n", b"error:163\n"])
def test_optional_setting_never_swallows_other_firmware_errors(reply):
    ser = _FakeSerial([reply + b"ok\n"])
    transport = SerialTransport(ser)

    with pytest.raises(ProtocolError, match="GRBL error"):
        transport.send("$32=0", optional=True)
    transport.send("M5")

    assert ser.writes == [b"$32=0\n", b"M5\n"]


def test_unacknowledged_send_blocks_motion_even_if_late_ok_arrives():
    ser = _FakeSerial()
    transport = SerialTransport(ser)
    transport.send("G90", wait_ok=False)
    ser.responses.append(b"ok\n")

    with pytest.raises(ProtocolError, match="unsynchronized; reconnect"):
        transport.send("G0 X1")
    with pytest.raises(ProtocolError, match="unsynchronized; reconnect"):
        transport.send("M5")

    assert ser.writes == [b"G90\n", b"M5\n"]


def test_input_reset_and_healthy_status_cannot_restore_lost_ack_ownership(mocker):
    ser = _FakeSerial([b"o"])
    transport = SerialTransport(ser)
    mocker.patch.object(transport_mod.time, "sleep")
    with pytest.raises(DeviceTimeout, match="GRBL not responding"):
        transport.send("G90")

    transport.reset_input_buffer()
    ser.responses.append(b"<Idle|WPos:0,0>\n")
    status = transport.query_status()
    with pytest.raises(ProtocolError, match="unsynchronized; reconnect"):
        transport.send("G0 X1")

    assert status == "<Idle|WPos:0,0>"
    assert ser.writes == [b"G90\n", b"?"]


@pytest.mark.parametrize("operation", ["read", "write"])
def test_serial_io_failure_blocks_followup_motion_but_allows_emergency_release(
    mocker, operation
):
    ser = _FakeSerial([b"ok\n"])
    transport = SerialTransport(ser)
    native = getattr(ser, operation)

    fault = mocker.patch.object(
        ser, operation, side_effect=OSError("serial disconnected")
    )
    with pytest.raises(OSError, match="serial disconnected"):
        transport.send("G90")
    fault.side_effect = native
    writes_after_failure = list(ser.writes)

    with pytest.raises(ProtocolError, match="unsynchronized; reconnect"):
        transport.send("G0 X1")
    with pytest.raises(ProtocolError, match="unsynchronized; reconnect"):
        transport.send("M5")

    assert ser.writes == writes_after_failure + [b"M5\n"]


def test_diagnostic_chatter_stops_at_default_twelve_second_budget(mocker):
    clock = SimpleNamespace(now=0.0)
    ser = _FakeSerial([b"[MSG:busy]\n"] * 8)
    native_read = ser.read

    def read(n):
        clock.now += ser.timeout
        return native_read(n)

    mocker.patch.object(ser, "read", side_effect=read)
    mocker.patch.object(
        transport_mod, "time", SimpleNamespace(monotonic=lambda: clock.now)
    )
    transport = SerialTransport(ser)

    with pytest.raises(DeviceTimeout, match="GRBL not responding"):
        transport.send("G90")

    assert clock.now == 12.0
    assert ser.timeout == 3.0


_REPLY_STREAM = b"[MSG:ready]\r\nok\r\nerror:23\r\nok\n"


def _fragment_reply(cuts):
    boundaries = [0, *sorted(cuts), len(_REPLY_STREAM)]
    return [_REPLY_STREAM[start:end] for start, end in pairwise(boundaries)]


@given(cuts=st.sets(st.integers(min_value=1, max_value=len(_REPLY_STREAM) - 1)))
def test_arbitrary_serial_chunking_preserves_command_reply_ownership(cuts):
    ser = _FakeSerial(_fragment_reply(cuts))
    transport = SerialTransport(ser)

    transport.send("G90")
    with pytest.raises(ProtocolError, match="error:23.*G0 X1"):
        transport.send("G0 X1")
    transport.send("M5")

    assert ser.writes == [b"G90\n", b"G0 X1\n", b"M5\n"]


def test_split_ack_can_resume_after_transient_empty_reads():
    ser = _FakeSerial([b"", b"", b"", b"[MSG:busy]\n", b"o", b"", b"", b"", b"k\n"])
    transport = SerialTransport(ser)

    transport.send("G90")

    assert ser.writes == [b"G90\n"]


def test_four_consecutive_empty_reads_refuse_late_ack():
    ser = _FakeSerial([b""] * 4 + [b"ok\n"])
    transport = SerialTransport(ser)

    with pytest.raises(DeviceTimeout, match="GRBL not responding"):
        transport.send("G90")
    with pytest.raises(ProtocolError, match="unsynchronized; reconnect"):
        transport.send("G0 X1")

    assert ser.writes == [b"G90\n"]


@pytest.mark.parametrize(
    "native_timeout,first_budget",
    [(None, 0.3), (0.25, 0.25), (3.0, 0.3)],
    ids=["None", "0.25", "3.0"],
)
def test_partial_reads_use_remaining_budget_even_with_unbounded_serial_timeout(
    mocker, native_timeout, first_budget
):
    ser = _FakeSerial([b"o", b"k\n"])
    ser.timeout = native_timeout
    native_read = ser.read
    clock = SimpleNamespace(now=100.0)
    budgets = []
    delays = iter([0.2, 0.05])

    def read(n):
        budgets.append(ser.timeout)
        clock.now += next(delays)
        return native_read(n)

    mocker.patch.object(ser, "read", side_effect=read)
    mocker.patch.object(
        transport_mod, "time", SimpleNamespace(monotonic=lambda: clock.now)
    )
    transport = SerialTransport(ser)
    transport.RESPONSE_TIMEOUT_SECONDS = 0.3

    transport.send("G90")

    assert budgets == pytest.approx([first_budget, 0.1])
    assert ser.timeout == native_timeout


def test_expiry_between_clock_checks_prevents_native_read(mocker):
    ser = _FakeSerial([b"ok\n"])
    ticks = count(1)
    mocker.patch.object(
        transport_mod, "time", SimpleNamespace(monotonic=lambda: next(ticks))
    )
    read = mocker.spy(ser, "read")
    transport = SerialTransport(ser)
    transport.RESPONSE_TIMEOUT_SECONDS = 3

    with pytest.raises(DeviceTimeout, match="GRBL not responding"):
        transport.send("G90")

    read.assert_not_called()


def test_fragmented_oversized_reply_cannot_exceed_native_read_bound():
    ser = _FakeSerial([b"x" * 4095, b"xok\n"])
    transport = SerialTransport(ser)

    with pytest.raises(ProtocolError, match="reply exceeds 4096 bytes"):
        transport.send("G90")

    assert b"".join(ser.responses) == b"ok\n"


@pytest.mark.parametrize("noise", [b"noise>\n", b"<Idle\n", b"<Idle|WPos:\xff>\n"])
def test_status_skips_invalid_frames_until_complete_valid_report(mocker, noise):
    ser = _FakeSerial([noise + b"<Run|WPos:1,2>\n"])
    mocker.patch.object(transport_mod.time, "sleep")

    status = SerialTransport(ser).query_status()

    assert status == "<Run|WPos:1,2>"
    assert ser.writes == [b"?"]


@pytest.mark.parametrize("native_timeout, expected_budget", [(None, 12.0), (3.0, 3.0)])
def test_status_diagnostics_exhaust_budget_without_reporting_a_position(
    mocker, native_timeout, expected_budget
):
    ser = _FakeSerial([b"[MSG:busy]\n"] * 20)
    ser.timeout = native_timeout
    native_read = ser.read
    clock = SimpleNamespace(now=0.0)

    def read(n):
        clock.now += 1.0
        return native_read(n)

    def sleep(seconds):
        clock.now += seconds

    mocker.patch.object(ser, "read", side_effect=read)
    mocker.patch.object(
        transport_mod, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep)
    )

    status = SerialTransport(ser).query_status()

    assert status == ""
    assert clock.now == pytest.approx(expected_budget + 0.1)
    assert ser.timeout == native_timeout


def test_startup_returns_complete_available_banner_with_replacement_noise_ignored():
    banner = b"Grbl 1.1\n" + b"[MSG:booting]\n" * 30 + b"\xff"
    ser = _FakeSerial([banner])
    transport = SerialTransport(ser)

    startup = transport.read_startup()

    assert startup == "Grbl 1.1\n" + "[MSG:booting]\n" * 30
    assert ser.writes == []


def test_status_budget_expiring_after_diagnostic_returns_no_position(mocker):
    ser = _FakeSerial([b"[MSG:busy]\n"])
    ser.timeout = 5.0
    ticks = count(1)
    mocker.patch.object(
        transport_mod,
        "time",
        SimpleNamespace(monotonic=lambda: next(ticks), sleep=lambda _: None),
    )

    status = SerialTransport(ser).query_status()

    assert status == ""


# Accepted mutation survivors are cosmetic diagnostics, False -> None flags
# (only truthiness is observed), and equivalent outer deadline comparisons.
# A one-byte change to the startup cap or idle-read chunk size does not change
# the safety contract; bounded reads and decoded output are asserted instead.
