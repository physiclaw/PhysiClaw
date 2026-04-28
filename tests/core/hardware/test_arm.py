"""Tests for `physiclaw.core.hardware.arm` — Phase 5 hardware fakes.

`serial.Serial` is replaced with a `FakeSerial` that records writes and
returns scripted replies. GRBL's textual line protocol is small enough
that a few patterns cover every public method:

  - `?`          → status reply, e.g. `<Idle|WPos:0.000,0.000>`
  - any G-code   → `ok\n` or `error:N\n` or `ALARM:N\n`
"""
from __future__ import annotations

from collections import deque
from typing import Iterable

import pytest

from physiclaw.core.hardware import arm as arm_mod
from physiclaw.core.hardware.arm import StylusArm


pytestmark = [pytest.mark.integration]


# ---------- FakeSerial ----------


class FakeSerial:
    """Minimal in-memory replacement for `serial.Serial`.

    `responses` is an iterable of byte sequences returned by successive
    `readline()` calls. `?` queries pull from `status_replies`. Status
    queries use `read(in_waiting)` rather than `readline`, so we keep
    those bytes separate.
    """

    def __init__(
        self,
        *,
        responses: Iterable[bytes] = (),
        status_replies: Iterable[bytes] = (),
        startup_bytes: bytes = b"",
    ):
        self.writes: list[bytes] = []
        self.responses = deque(responses)
        self.status_replies = deque(status_replies)
        self._startup = startup_bytes
        self._startup_consumed = False
        self.in_waiting = len(startup_bytes)
        self.closed = False

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        # `?` is a status query — caller will then read(in_waiting) for the response.
        if data == b"?":
            if self.status_replies:
                self._next_status = self.status_replies.popleft()
            else:
                self._next_status = b""
            self.in_waiting = len(self._next_status)
        return len(data)

    def readline(self) -> bytes:
        if self.responses:
            return self.responses.popleft()
        return b""

    def read(self, n: int) -> bytes:
        if not self._startup_consumed and self._startup:
            self._startup_consumed = True
            self.in_waiting = 0
            return self._startup
        out = getattr(self, "_next_status", b"")
        self.in_waiting = 0
        self._next_status = b""
        return out

    def reset_input_buffer(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def _arm(
    mocker, *,
    responses: Iterable[bytes] = (),
    status_replies: Iterable[bytes] = (),
) -> tuple[StylusArm, FakeSerial]:
    """Build a StylusArm with FakeSerial as its serial backend, no setup()."""
    fake = FakeSerial(
        responses=list(responses), status_replies=list(status_replies),
    )
    mocker.patch.object(arm_mod, "detect_grbl", return_value="/dev/cu.fake")
    mocker.patch.object(arm_mod.serial, "Serial", return_value=fake)
    mocker.patch.object(arm_mod.time, "sleep")  # collapse the 2s settle
    return StylusArm(), fake


# ---------- __init__ ----------


def test_init_uses_detect_grbl_when_port_omitted(mocker) -> None:
    fake = FakeSerial()
    detect_spy = mocker.patch.object(
        arm_mod, "detect_grbl", return_value="/dev/cu.test",
    )
    serial_spy = mocker.patch.object(
        arm_mod.serial, "Serial", return_value=fake,
    )
    mocker.patch.object(arm_mod.time, "sleep")

    arm = StylusArm()

    assert arm.port == "/dev/cu.test"
    detect_spy.assert_called_once()
    serial_spy.assert_called_once_with("/dev/cu.test", 115200, timeout=3)


def test_init_raises_when_no_grbl_detected(mocker) -> None:
    mocker.patch.object(arm_mod, "detect_grbl", return_value=None)

    with pytest.raises(Exception, match="GRBL device not found"):
        StylusArm()


def test_init_uses_explicit_port_when_provided(mocker) -> None:
    fake = FakeSerial()
    serial_spy = mocker.patch.object(
        arm_mod.serial, "Serial", return_value=fake,
    )
    mocker.patch.object(arm_mod.time, "sleep")
    detect_spy = mocker.patch.object(arm_mod, "detect_grbl")

    arm = StylusArm(port="/dev/cu.explicit", baudrate=9600)

    assert arm.port == "/dev/cu.explicit"
    detect_spy.assert_not_called()
    serial_spy.assert_called_once_with("/dev/cu.explicit", 9600, timeout=3)


# ---------- _send ----------


def test_send_writes_command_with_crlf(mocker) -> None:
    arm, fake = _arm(mocker, responses=[b"ok\n"])

    arm._send("G0 X1 Y1")

    assert fake.writes[-1] == b"G0 X1 Y1\r\n"


def test_send_returns_immediately_when_wait_ok_false(mocker) -> None:
    arm, fake = _arm(mocker)  # no responses queued

    arm._send("G0 X0 Y0", wait_ok=False)


def test_send_skips_blank_lines_until_ok(mocker) -> None:
    arm, _ = _arm(mocker, responses=[b"\n", b"ok\n"])

    arm._send("G0 X0 Y0")  # must not raise


def test_send_raises_after_too_many_empty_lines(mocker) -> None:
    arm, _ = _arm(mocker, responses=[b""] * 10)

    with pytest.raises(Exception, match="GRBL not responding"):
        arm._send("G0 X0 Y0")


def test_send_raises_on_error_response(mocker) -> None:
    arm, _ = _arm(mocker, responses=[b"error:23\n"])

    with pytest.raises(Exception, match="GRBL error"):
        arm._send("G0 X0 Y0")


def test_send_raises_on_alarm_response(mocker) -> None:
    arm, _ = _arm(mocker, responses=[b"ALARM:1\n"])

    with pytest.raises(Exception, match="GRBL alarm"):
        arm._send("G0 X0 Y0")


# ---------- _query_status ----------


def test_query_status_returns_status_line(mocker) -> None:
    arm, _ = _arm(
        mocker, status_replies=[b"<Idle|WPos:0.000,0.000>\r\n"],
    )

    line = arm._query_status()

    assert line.startswith("<")


def test_query_status_returns_empty_when_no_response(mocker) -> None:
    arm, _ = _arm(mocker, status_replies=[b""])

    assert arm._query_status() == ""


# ---------- wait_idle ----------


def test_wait_idle_returns_when_idle(mocker) -> None:
    arm, _ = _arm(
        mocker, status_replies=[b"<Idle|WPos:0.000,0.000>\n"],
    )

    arm.wait_idle()  # no raise


def test_wait_idle_polls_until_idle(mocker) -> None:
    arm, _ = _arm(
        mocker,
        status_replies=[b"<Run|WPos:1.0,1.0>\n", b"<Idle|WPos:1.0,1.0>\n"],
    )

    arm.wait_idle()


def test_wait_idle_raises_on_timeout(mocker) -> None:
    arm, _ = _arm(
        mocker,
        status_replies=[b"<Run|WPos:0,0>\n"] * 50,
    )
    # Force time.time() to advance past the deadline.
    times = iter([0.0, 100.0, 100.0])
    mocker.patch.object(arm_mod.time, "time", side_effect=lambda: next(times))

    with pytest.raises(RuntimeError, match="Arm not idle"):
        arm.wait_idle(timeout=1)


# ---------- position ----------


def test_position_parses_wpos(mocker) -> None:
    arm, _ = _arm(
        mocker, status_replies=[b"<Idle|WPos:1.500,2.500|FS:0,0>\n"],
    )

    x, y = arm.position()

    assert x == 1.5 and y == 2.5


def test_position_falls_back_to_mpos_minus_wco(mocker) -> None:
    arm, _ = _arm(
        mocker,
        status_replies=[b"<Idle|MPos:5.000,7.000|WCO:1.000,2.000>\n"],
    )

    x, y = arm.position()

    assert x == 4.0 and y == 5.0


def test_position_uses_mpos_only_with_warning(
    mocker, caplog: pytest.LogCaptureFixture,
) -> None:
    import logging
    arm, _ = _arm(
        mocker, status_replies=[b"<Idle|MPos:3.000,4.000>\n"],
    )

    with caplog.at_level(logging.WARNING, logger="physiclaw.core.hardware.arm"):
        x, y = arm.position()

    assert (x, y) == (3.0, 4.0)
    assert any("not reporting WPos" in r.getMessage() for r in caplog.records)


def test_position_raises_when_unparseable(mocker) -> None:
    arm, _ = _arm(mocker, status_replies=[b"<Idle>\n"])

    with pytest.raises(RuntimeError, match="Cannot parse position"):
        arm.position()


# ---------- setup / unlock / set_origin / return_to_origin ----------


def test_setup_unlocks_when_alarm_present(mocker) -> None:
    # Setup runs: VERSION → status → SET_ORIGIN → MM → ABS → F → IDLE → $10=0
    # If alarm detected, also unlock between status and SET_ORIGIN.
    arm, fake = _arm(
        mocker,
        # Responses: $I + $X (unlock) + 6 setup commands = 8 ok's
        responses=[b"ok\n"] * 8,
        status_replies=[b"<Alarm|WPos:0,0>\n"],
    )

    arm.setup()

    written = b"".join(fake.writes)
    assert b"$X" in written
    assert b"$I" in written
    assert b"G92" in written
    assert b"$10=0" in written


def test_setup_skips_unlock_when_no_alarm(mocker) -> None:
    arm, fake = _arm(
        mocker,
        responses=[b"ok\n"] * 7,  # no $X
        status_replies=[b"<Idle|WPos:0,0>\n"],
    )

    arm.setup()

    assert b"$X\r\n" not in fake.writes


def test_unlock_sends_X(mocker) -> None:
    arm, fake = _arm(mocker, responses=[b"ok\n"])

    arm.unlock()

    assert any(b"$X" in w for w in fake.writes)


def test_set_origin_sends_G92(mocker) -> None:
    arm, fake = _arm(mocker, responses=[b"ok\n"])

    arm.set_origin()

    assert any(b"G92" in w for w in fake.writes)


def test_return_to_origin_fast_moves_and_waits(mocker) -> None:
    arm, fake = _arm(
        mocker, responses=[b"ok\n"], status_replies=[b"<Idle|WPos:0,0>\n"],
    )

    arm.return_to_origin()

    assert any(b"X0.000Y0.000" in w for w in fake.writes)


# ---------- direction mapping ----------


def test_set_direction_mapping_builds_8_directions() -> None:
    arm = StylusArm.__new__(StylusArm)  # bypass __init__
    arm.set_direction_mapping(right_vec=(1.0, 0.0), down_vec=(0.0, 1.0))

    # 8 cardinal + diagonal entries.
    assert set(arm.MOVE_DIRECTIONS.keys()) == {
        "right", "left", "bottom", "top",
        "top-left", "top-right", "bottom-left", "bottom-right",
    }
    assert arm.MOVE_DIRECTIONS["right"] == (1.0, 0.0)
    assert arm.MOVE_DIRECTIONS["left"] == (-1.0, -0.0)
    assert arm.MOVE_DIRECTIONS["bottom"] == (0.0, 1.0)
    assert arm.MOVE_DIRECTIONS["top"] == (-0.0, -1.0)


# ---------- pen / motion primitives ----------


def test_pen_down_raises_when_z_unset(mocker) -> None:
    arm, _ = _arm(mocker, responses=[])

    with pytest.raises(RuntimeError, match="Z_DOWN not set"):
        arm._pen_down()


def test_pen_down_uses_calibrated_z(mocker) -> None:
    arm, fake = _arm(mocker, responses=[b"ok\n"])
    arm.Z_DOWN = -2.5

    arm._pen_down()

    assert any(b"Z-2.5" in w for w in fake.writes)


def test_pen_down_accepts_override_z(mocker) -> None:
    arm, fake = _arm(mocker, responses=[b"ok\n"])

    arm._pen_down(z=-3.0, speed=4000)

    assert any(b"Z-3.0" in w and b"F4000" in w for w in fake.writes)


def test_pen_up_drives_z_to_zero(mocker) -> None:
    arm, fake = _arm(mocker, responses=[b"ok\n"])

    arm._pen_up()

    assert any(b"Z0.0" in w for w in fake.writes)


def test_dwell_emits_G4(mocker) -> None:
    arm, fake = _arm(mocker, responses=[b"ok\n"])

    arm._dwell(0.5)

    assert any(b"G4 P0.5" in w for w in fake.writes)


def test_fast_move_emits_G0(mocker) -> None:
    arm, fake = _arm(mocker, responses=[b"ok\n"])

    arm._fast_move(1.5, 2.5, speed=5000)

    cmd = next(w for w in fake.writes if b"G0" in w)
    assert b"X1.500" in cmd
    assert b"Y2.500" in cmd
    assert b"F5000" in cmd


def test_linear_move_emits_G1(mocker) -> None:
    arm, fake = _arm(mocker, responses=[b"ok\n"])

    arm._linear_move(0.0, 1.0)

    assert any(b"G1 X0.000Y1.000" in w for w in fake.writes)


# ---------- _hold_contact ----------


def test_hold_contact_short_does_not_set_motors_always_on(mocker) -> None:
    arm, fake = _arm(
        mocker,
        # _pen_down + _dwell + _pen_up = 3 ok's
        responses=[b"ok\n"] * 3,
        status_replies=[b"<Idle|WPos:0,0>\n"],
    )
    arm.Z_DOWN = -2.0

    arm._hold_contact(0.05)  # under 0.2s threshold

    assert b"$1=255\r\n" not in fake.writes


def test_hold_contact_long_keeps_motors_on(mocker) -> None:
    arm, fake = _arm(
        mocker,
        # $1=255 + pen_down + dwell + pen_up + $1=250 = 5 ok's
        responses=[b"ok\n"] * 5,
        status_replies=[b"<Idle|WPos:0,0>\n"],
    )
    arm.Z_DOWN = -2.0

    arm._hold_contact(0.5)

    written = b"".join(fake.writes)
    assert b"$1=255" in written
    assert b"$1=250" in written


def test_hold_contact_restores_motors_even_on_failure(mocker) -> None:
    arm, fake = _arm(
        mocker,
        # $1=255 ok, then pen_down raises (no further ok), $1=250 retry...
        responses=[b"ok\n", b"error:1\n"] + [b"ok\n"] * 5,
        status_replies=[],
    )
    arm.Z_DOWN = -2.0

    with pytest.raises(Exception):
        arm._hold_contact(0.5)

    written = b"".join(fake.writes)
    # Restore call still happened.
    assert b"$1=250" in written


# ---------- _set_motors_always_on ----------


def test_set_motors_always_on_retries_via_unlock(mocker) -> None:
    arm, fake = _arm(
        mocker,
        # First $1=255 errors → unlock + $1=255 retry succeeds.
        responses=[b"error:1\n", b"ok\n", b"ok\n"],
    )

    arm._set_motors_always_on(True)

    written = b"".join(fake.writes)
    assert b"$X" in written
    assert b"$1=255" in written


# ---------- public gestures ----------


def test_tap_dispatches_short_hold(mocker) -> None:
    arm, _ = _arm(
        mocker,
        responses=[b"ok\n"] * 3,
        status_replies=[b"<Idle|WPos:0,0>\n"],
    )
    arm.Z_DOWN = -2.0

    arm.tap()


def test_double_tap_dispatches(mocker) -> None:
    arm, fake = _arm(
        mocker,
        # 6 commands: pen_down, dwell, pen_up, dwell, pen_down, dwell, pen_up,
        # then status query for wait_idle.
        responses=[b"ok\n"] * 7,
        status_replies=[b"<Idle|WPos:0,0>\n"],
    )
    arm.Z_DOWN = -2.0

    arm.double_tap()

    # Two pen_down emits in the wire log.
    z_down_count = sum(1 for w in fake.writes if b"Z-2.0F" in w)
    assert z_down_count == 2


def test_long_press_dispatches(mocker) -> None:
    arm, _ = _arm(
        mocker,
        responses=[b"ok\n"] * 5,
        status_replies=[b"<Idle|WPos:0,0>\n"],
    )
    arm.Z_DOWN = -2.0

    arm.long_press()


def test_swipe_emits_relative_linear_move(mocker) -> None:
    arm, fake = _arm(mocker, responses=[b"ok\n"] * 4)
    arm.Z_DOWN = -2.0
    arm.set_direction_mapping(right_vec=(1.0, 0.0), down_vec=(0.0, 1.0))

    arm.swipe("right", speed="fast")

    written = b"".join(fake.writes)
    assert b"G91G1" in written
    assert b"F10000" in written  # fast speed


def test_swipe_raises_when_directions_unset(mocker) -> None:
    arm, _ = _arm(mocker, responses=[])

    with pytest.raises(RuntimeError, match="MOVE_DIRECTIONS not set"):
        arm.swipe("right")


# ---------- move ----------


def test_move_emits_relative_fast_move(mocker) -> None:
    arm, fake = _arm(
        mocker, responses=[b"ok\n"] * 2,
        status_replies=[b"<Idle|WPos:0,0>\n"],
    )
    arm.set_direction_mapping(right_vec=(1.0, 0.0), down_vec=(0.0, 1.0))

    arm.move("right", "small")

    written = b"".join(fake.writes)
    assert b"G91G0" in written
    assert b"X3" in written  # small distance = 3mm


def test_move_normalizes_diagonal(mocker) -> None:
    arm, fake = _arm(
        mocker, responses=[b"ok\n"] * 2,
        status_replies=[b"<Idle|WPos:0,0>\n"],
    )
    arm.set_direction_mapping(right_vec=(1.0, 0.0), down_vec=(0.0, 1.0))

    arm.move("top-right", "medium")

    # Diagonal distance should be normalized to 8mm magnitude.
    written = b"".join(fake.writes)
    # Each axis gets 8/sqrt(2) ≈ 5.657 mm.
    assert b"5.657" in written or b"-5.657" in written


def test_move_raises_when_directions_unset(mocker) -> None:
    arm, _ = _arm(mocker, responses=[])

    with pytest.raises(RuntimeError, match="MOVE_DIRECTIONS not set"):
        arm.move("right")


# ---------- close ----------


def test_close_releases_motors_and_closes_serial(mocker) -> None:
    arm, fake = _arm(mocker, responses=[b"ok\n"])

    arm.close()

    assert fake.closed is True


def test_close_swallows_motor_release_failure(mocker) -> None:
    arm, fake = _arm(
        mocker,
        # $1=250 errors three times → outer try suppresses.
        responses=[b"error:1\n", b"ok\n"] * 6,  # endless cycle
    )

    # Even if motor restore retries, close() must not raise.
    arm.close()

    assert fake.closed is True
