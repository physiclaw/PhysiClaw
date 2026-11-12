"""Tests for `physiclaw.core.hardware.solenoid` — the Z-actuator driver.

`Solenoid` depends only on two transport callables (`send` + `dwell`), so we
exercise it with a tiny recorder that captures the exact command sequence —
no serial port, no GRBL. Wire-level integration (solenoid G-code reaching the
serial port through StylusArm) is covered by the gesture tests in test_arm.py.
"""

from __future__ import annotations

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from physiclaw.core.hardware.solenoid import Solenoid


class Boom(RuntimeError):
    """Injected transport failure (e.g. GRBL alarm / serial timeout)."""


class Recorder:
    """Captures the ordered (op, *args) calls a Solenoid makes.

    ``fail_on`` is an optional predicate ``(op, payload) -> bool``; when it
    returns True the corresponding ``send``/``dwell`` raises :class:`Boom`,
    simulating a mid-gesture GRBL error so we can prove the coil is forced off.
    """

    def __init__(self, fail_on=None) -> None:
        self.calls: list[tuple] = []
        self._fail_on = fail_on

    @property
    def sends(self) -> list[str]:
        return [c[1] for c in self.calls if c[0] == "send"]

    def send(self, cmd: str, optional: bool = False) -> None:
        self.calls.append(("send", cmd, optional))
        if self._fail_on and self._fail_on("send", cmd):
            raise Boom(f"transport failed on {cmd!r}")

    def dwell(self, seconds: float) -> None:
        self.calls.append(("dwell", seconds))
        if self._fail_on and self._fail_on("dwell", seconds):
            raise Boom(f"transport failed on dwell {seconds}")


@pytest.fixture
def rec() -> Recorder:
    return Recorder()


@pytest.fixture
def sol(rec: Recorder) -> Solenoid:
    return Solenoid(send=rec.send, dwell=rec.dwell)


@pytest.mark.parametrize("method", ["tap", "double_tap", "press_and_hold"])
@pytest.mark.parametrize(
    "duration", [-1, float("nan"), float("inf"), True, "0.1", 10**1000]
)
def test_invalid_duration_cannot_energize_coil(rec, sol, method, duration):
    with pytest.raises(ValueError, match="seconds|dwell"):
        getattr(sol, method)(duration)
    assert rec.calls == []


@pytest.mark.parametrize("method", ["tap", "double_tap"])
@pytest.mark.parametrize("duration", [0.151, 1.2, 60, 1e100])
def test_long_taps_cannot_sustain_strike_current(rec, sol, method, duration):
    with pytest.raises(ValueError, match="press_and_hold"):
        getattr(sol, method)(duration)
    assert rec.calls == []


@pytest.mark.parametrize("method", ["tap", "double_tap"])
def test_calibrated_assistive_touch_duration_is_allowed(rec, sol, method):
    getattr(sol, method)(0.15)
    assert ("dwell", 0.15) in rec.calls
    assert rec.sends[-1] == "M5"


# ---------- configure ----------


def test_configure_sets_pwm_mode_and_range_optionally_then_coil_off(
    rec: Recorder, sol: Solenoid
) -> None:
    sol.configure()

    assert rec.calls == [
        ("send", "$32=0", True),  # spindle mode, optional
        ("send", "$30=1000", True),  # S-range, optional
        ("send", "M5", False),  # failsafe coil-off
    ]


# ---------- press / release ----------


def test_press_strikes_settles_then_holds(rec: Recorder, sol: Solenoid) -> None:
    sol.press()

    assert rec.calls == [
        ("send", "M3 S1000", False),  # strike at HIT_S
        ("dwell", 0.08),  # settle before reducing strike current
        ("send", "M3 S750", False),  # drop to HOLD_S, coil left energized
    ]


def test_release_cuts_then_waits_for_rebound(rec: Recorder, sol: Solenoid) -> None:
    sol.release()

    assert rec.calls == [
        ("send", "M5", False),  # cut the coil
        ("dwell", 0.2),  # wait for the spring to lift
    ]


# ---------- tap / press_and_hold ----------


def test_tap_fires_for_duration_then_releases(rec: Recorder, sol: Solenoid) -> None:
    sol.tap(0.08)

    assert rec.calls == [
        ("send", "M3 S1000", False),  # strike at HIT_S
        ("dwell", 0.08),  # contact duration
        ("send", "M5", False),  # release (via release())
        ("dwell", 0.2),  # spring rebound
    ]
    # No hold step for a momentary tap.
    assert ("send", "M3 S750", False) not in rec.calls


def test_double_tap_uses_short_gap_then_full_release(
    rec: Recorder, sol: Solenoid
) -> None:
    sol.double_tap(0.08)

    assert rec.calls == [
        ("send", "M3 S1000", False),  # first strike
        ("dwell", 0.08),  # contact duration
        ("send", "M5", False),  # lift
        ("dwell", 0.1),  # brief gap — break contact only
        ("send", "M3 S1000", False),  # second strike
        ("dwell", 0.08),  # contact duration
        ("send", "M5", False),  # release
        ("dwell", 0.2),  # full spring-clear after the pair
    ]
    # The inter-tap gap must stay below a full release, or down-to-down drifts
    # past the iOS double-tap window and the pair reads as two single taps.
    assert Solenoid.DOUBLE_TAP_GAP_MS < Solenoid.RELEASE_MS


def test_double_tap_forces_coil_off_when_a_strike_fails(rec: Recorder) -> None:
    # A transport failure mid-double-tap must still force the coil off.
    rec._fail_on = lambda op, p: op == "send" and p == "M3 S1000"
    sol = Solenoid(send=rec.send, dwell=rec.dwell)

    with pytest.raises(Boom, match="M3 S1000"):
        sol.double_tap(0.08)

    assert rec.sends[-1] == "M5"  # emergency release fired


def test_press_and_hold_drops_to_hold_current_for_duration(
    rec: Recorder, sol: Solenoid
) -> None:
    sol.press_and_hold(1.2)

    assert rec.calls == [
        ("send", "M3 S1000", False),  # strike at HIT_S
        ("dwell", 0.08),  # settle
        ("send", "M3 S750", False),  # hold current
        ("dwell", 1.2),  # hold duration
        ("send", "M5", False),  # release
        ("dwell", 0.2),  # spring rebound
    ]


# ---------- formatting uses the configured S-values ----------


def test_duty_levels_track_class_constants(rec: Recorder) -> None:
    class HotSolenoid(Solenoid):
        HIT_S = 800
        HOLD_S = 120

    HotSolenoid(send=rec.send, dwell=rec.dwell).press_and_hold(0.5)

    assert "M3 S800" in rec.sends  # HIT_S override honored
    assert "M3 S120" in rec.sends  # HOLD_S override honored


# ---------- construction guard: hold current must be safe ----------


@pytest.mark.parametrize("hold", [1000, 1500, 0, -50])
def test_init_rejects_unsafe_hold_current(rec: Recorder, hold: int) -> None:
    """A hold at/above the strike duty (or non-positive) would burn the coil —
    reject it at construction so a misconfigured rig never energizes."""

    class BadSolenoid(Solenoid):
        HOLD_S = hold  # HIT_S stays 1000

    with pytest.raises(ValueError, match="HOLD_S"):
        BadSolenoid(send=rec.send, dwell=rec.dwell)


@pytest.mark.parametrize("hold", [1, 999])
def test_init_accepts_hold_below_hit(rec: Recorder, hold: int) -> None:
    class OkSolenoid(Solenoid):
        HOLD_S = hold  # Both open-boundary neighbors are valid configuration.

    sol = OkSolenoid(send=rec.send, dwell=rec.dwell)

    sol.press()

    assert rec.sends == ["M3 S1000", f"M3 S{hold}"]


# ---------- coil is forced off on any mid-gesture failure ----------


def test_tap_forces_coil_off_when_dwell_raises(rec: Recorder) -> None:
    rec._fail_on = lambda op, _p: op == "dwell"  # GRBL stalls during the tap
    sol = Solenoid(send=rec.send, dwell=rec.dwell)

    with pytest.raises(Boom):
        sol.tap(0.08)

    assert rec.sends[0] == "M3 S1000"  # coil was energized
    assert rec.sends[-1] == "M5"  # ...and forced off despite the failure


def test_press_forces_coil_off_if_hold_send_raises(rec: Recorder) -> None:
    # Fail the drop-to-hold write, after the peak strike already turned the coil on.
    rec._fail_on = lambda op, p: op == "send" and p == "M3 S750"
    sol = Solenoid(send=rec.send, dwell=rec.dwell)

    with pytest.raises(Boom):
        sol.press()

    # Strike on, hold attempt failed, then forced off — never stuck at peak.
    assert rec.sends == ["M3 S1000", "M3 S750", "M5"]


def test_held_forces_coil_off_when_block_raises() -> None:
    rec = Recorder()
    sol = Solenoid(send=rec.send, dwell=rec.dwell)

    def run_operation():
        assert rec.sends[-1] == "M3 S750"  # held at hold current inside block
        raise ValueError("slide failed")

    with pytest.raises(ValueError, match="slide failed"), sol.held():
        run_operation()

    assert rec.sends[-1] == "M5"  # released despite the error
    # The error path unwinds into rig.engaged()'s auto-park — the same
    # spring-clear dwell as release(), or the park G0 starts while the
    # tip is still on the glass and drags across the screen.
    assert ("dwell", Solenoid.RELEASE_MS / 1000.0) in rec.calls


def test_held_releases_normally_on_clean_exit(rec: Recorder) -> None:
    sol = Solenoid(send=rec.send, dwell=rec.dwell)

    with sol.held():
        pass

    # Clean exit takes the normal release: M5 + spring-rebound dwell.
    assert rec.sends[-1] == "M5"
    assert ("dwell", Solenoid.RELEASE_MS / 1000.0) in rec.calls


def test_force_off_failure_is_swallowed_and_original_error_survives(
    rec: Recorder,
) -> None:
    # The tap dwell fails AND the emergency M5 also fails: the original error
    # must still propagate (not be masked by the release failure).
    rec._fail_on = lambda op, p: op == "dwell" or (op == "send" and p == "M5")
    sol = Solenoid(send=rec.send, dwell=rec.dwell)

    with pytest.raises(Boom, match="dwell"):
        sol.tap(0.08)

    assert "M5" in rec.sends  # release was still attempted


# These are the bench-calibrated wire sequences, independent of the driver's
# constants. Reading SETTLE_MS / RELEASE_MS from the implementation in the
# expected result would let a broken unit conversion or changed timing pass.
_PRESS = [("send", "M3 S1000", False), ("dwell", 0.08), ("send", "M3 S750", False)]
_RELEASE = [("send", "M5", False), ("dwell", 0.2)]
_TAP = [("send", "M3 S1000", False), ("dwell", 0.08)]
_GESTURES = [
    ("press", (), _PRESS),
    ("tap", (0.08,), _TAP + _RELEASE),
    (
        "double_tap",
        (0.08,),
        _TAP + [("send", "M5", False), ("dwell", 0.1)] + _TAP + _RELEASE,
    ),
    ("press_and_hold", (1.2,), _PRESS + [("dwell", 1.2)] + _RELEASE),
]
_FAULT_CASES = [
    pytest.param(method, args, expected[:step], id=f"{method}-operation-{step}")
    for method, args, expected in _GESTURES
    for step in range(1, len(expected) + 1)
]


@pytest.mark.parametrize("method,args,attempted", _FAULT_CASES)
@pytest.mark.parametrize("error_type", [Boom, KeyboardInterrupt])
def test_gesture_fault_at_every_io_boundary_releases_and_preserves_error(
    method, args, attempted, error_type
):
    error = error_type("injected gesture failure")

    def fail_once(_op, _payload):
        if len(rec.calls) == len(attempted):
            raise error
        return False

    rec = Recorder(fail_on=fail_once)
    sol = Solenoid(send=rec.send, dwell=rec.dwell)

    with pytest.raises(error_type, match="injected gesture failure") as caught:
        getattr(sol, method)(*args)

    assert caught.value is error
    assert rec.calls == attempted + _RELEASE


@pytest.mark.parametrize("error_type", [Boom, KeyboardInterrupt])
def test_held_body_failure_releases_before_propagating_original_error(
    rec, sol, error_type
):
    error = error_type("slide interrupted")

    with pytest.raises(error_type, match="slide interrupted") as caught, sol.held():
        raise error

    assert caught.value is error
    assert rec.calls == _PRESS + _RELEASE


@pytest.mark.parametrize("method", ["tap", "double_tap"])
def test_tap_first_float_above_maximum_never_energizes(rec, sol, method):
    duration = math.nextafter(0.15, math.inf)

    with pytest.raises(ValueError, match="press_and_hold"):
        getattr(sol, method)(duration)

    assert rec.calls == []


@pytest.mark.parametrize("method", ["tap", "double_tap"])
@given(duration=st.floats(min_value=0, max_value=0.15))
def test_tap_safe_duration_preserves_contact_and_finishes_with_rebound(
    method, duration
):
    rec = Recorder()
    sol = Solenoid(send=rec.send, dwell=rec.dwell)

    getattr(sol, method)(duration)

    assert rec.calls[:2] == [("send", "M3 S1000", False), ("dwell", duration)]
    assert rec.calls[-2:] == _RELEASE


# Accepted mutation survivors: diagnostic text can change without changing the
# exception contract; False -> None in the completion flag is equivalent because
# it is only tested for truth. Do not couple safety tests to either spelling.
# Callable type-alias mutations affect annotations, outside runtime behavior.
