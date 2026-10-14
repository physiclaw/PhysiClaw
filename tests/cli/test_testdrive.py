"""Tests for `physiclaw.cli.testdrive` — the post-flash bring-up check.

The arm is faked at the lazy-import boundary (`StylusArm` is imported
inside the command body) and the interactive command loop is fed through
CliRunner stdin — no serial hardware, no wall-clock waits.

Mutation review: 58 of 69 variants are detected. Accepted survivors only
change display text/style or replace the initial False completion flag with
equivalent falsy None. The EOF loop mutation fails deterministically on a
second input read, so the regression does not rely on a timeout.
"""

from __future__ import annotations

import importlib
import sys

import pytest
import typer
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from typer.testing import CliRunner

from physiclaw.core.hardware.device import DeviceNotFound, DeviceTimeout, ProtocolError

testdrive_mod = importlib.import_module("physiclaw.cli.testdrive")

app = typer.Typer()
app.command()(testdrive_mod.testdrive)
runner = CliRunner()


class FakeArm:
    """Records the command's hardware calls; no serial underneath."""

    def __init__(self) -> None:
        self.port = "/dev/cu.fake"
        self.jogs: list[tuple[float, float]] = []
        self.taps = 0
        self.long_presses = 0
        self.setup_called = False
        self.closed = False

    def setup(self) -> None:
        self.setup_called = True

    def jog(self, dx: float, dy: float) -> None:
        self.jogs.append((dx, dy))

    def tap(self) -> None:
        self.taps += 1

    def long_press(self) -> None:
        self.long_presses += 1

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_arm(mocker) -> FakeArm:
    """A FakeArm wired in place of StylusArm; no live server recorded."""
    arm = FakeArm()
    mocker.patch("physiclaw.core.hardware.arm.StylusArm", return_value=arm)
    mocker.patch("physiclaw.common.runtime_state.read_live", return_value=None)
    return arm


def _cmds(*commands: str) -> str:
    """Stdin feed for the interactive loop, one command per prompt."""
    return "".join(c + "\n" for c in commands)


# ---------- the command loop ----------


def test_moves_and_actions_execute(fake_arm) -> None:
    result = runner.invoke(
        app, [], input=_cmds("X20Y20", "Y-10", "tap", "longpress", "q")
    )

    assert result.exit_code == 0
    assert fake_arm.setup_called
    assert fake_arm.jogs == [(20.0, 20.0), (0.0, -10.0)]
    assert fake_arm.taps == 1
    assert fake_arm.long_presses == 1
    assert fake_arm.closed


def test_commands_ignore_case_and_spaces(fake_arm) -> None:
    result = runner.invoke(app, [], input=_cmds("x10 Y5", "TAP", "LongPress", "Q"))

    assert result.exit_code == 0
    assert fake_arm.jogs == [(10.0, 5.0)]
    assert fake_arm.taps == 1
    assert fake_arm.long_presses == 1


def test_decimal_and_single_axis_moves(fake_arm) -> None:
    result = runner.invoke(app, [], input=_cmds("X1.5Y-2.5", "X-10", "q"))

    assert result.exit_code == 0
    assert fake_arm.jogs == [(1.5, -2.5), (-10.0, 0.0)]


def test_unknown_input_shows_help_line(fake_arm) -> None:
    result = runner.invoke(app, [], input=_cmds("wat", "q"))

    assert result.exit_code == 0
    assert "try X10, Y10, X-10Y-10" in result.output
    assert fake_arm.jogs == []


def test_oversize_move_rejected(fake_arm) -> None:
    # Caps are inclusive and per-axis: exactly ±20 in X / ±50 in Y pass,
    # anything beyond either cap is refused (X20.5 shows X's cap is its
    # own, not Y's).
    result = runner.invoke(
        app, [], input=_cmds("X500", "X20.5", "Y50.5", "X20", "Y-50", "q")
    )

    assert result.exit_code == 0
    assert "within ±20 mm in X and ±50 mm in Y" in result.output
    assert fake_arm.jogs == [(20.0, 0.0), (0.0, -50.0)]


def test_axes_diagram_and_help_shown(fake_arm) -> None:
    result = runner.invoke(app, [], input=_cmds("q"))

    assert "origin" in result.output
    assert "[A]" in result.output
    assert "[B]" in result.output
    assert "Motors A and B" in result.output
    assert "q to finish" in result.output
    assert fake_arm.jogs == []
    assert fake_arm.closed  # port released on quit


# ---------- guards and error paths ----------


def test_refuses_when_server_live(mocker) -> None:
    mocker.patch("physiclaw.common.runtime_state.read_live", return_value={"pid": 42})
    arm_spy = mocker.patch("physiclaw.core.hardware.arm.StylusArm")

    result = runner.invoke(app, [])

    assert result.exit_code == 1
    assert "stop it first" in result.output
    assert "pid 42" in result.output
    arm_spy.assert_not_called()


def test_exits_plainly_when_board_missing(mocker) -> None:
    mocker.patch("physiclaw.common.runtime_state.read_live", return_value=None)
    mocker.patch(
        "physiclaw.core.hardware.arm.StylusArm",
        side_effect=DeviceNotFound("GRBL device not found — no serial ports"),
    )

    result = runner.invoke(app, [])

    assert result.exit_code == 1
    assert "GRBL device not found" in result.output


def test_board_dying_midrun_closes_port(fake_arm, mocker) -> None:
    mocker.patch.object(
        fake_arm, "jog", side_effect=ProtocolError("GRBL error: error:9")
    )

    result = runner.invoke(app, [], input=_cmds("x10"))

    assert result.exit_code == 1
    assert "stopped responding" in result.output
    assert fake_arm.closed


@pytest.mark.parametrize(
    "error", [OSError("serial disconnected"), DeviceTimeout("timeout")]
)
def test_regression_testdrive_connection_failure_is_a_clean_error(mocker, error):
    mocker.patch("physiclaw.common.runtime_state.read_live", return_value=None)
    factory = mocker.patch("physiclaw.core.hardware.arm.StylusArm", side_effect=error)

    result = runner.invoke(app, ["--port", "fake-port"])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert str(error) in result.stderr
    factory.assert_called_once_with("fake-port")


@pytest.mark.parametrize("operation", ["setup", "jog", "tap", "long_press"])
def test_regression_testdrive_serial_failure_closes_arm_and_reports_error(
    fake_arm, mocker, operation
):
    mocker.patch.object(fake_arm, operation, side_effect=OSError("serial disconnected"))
    close = mocker.spy(fake_arm, "close")

    result = runner.invoke(app, [], input=_cmds("x1", "tap", "longpress", "q"))

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "the board stopped responding" in result.stderr
    assert "serial disconnected" in result.stderr
    close.assert_called_once_with()


@pytest.mark.parametrize(
    "error", [OSError("close failed"), ProtocolError("close failed")]
)
def test_regression_testdrive_close_failure_is_a_clean_error(fake_arm, mocker, error):
    close = mocker.patch.object(fake_arm, "close", side_effect=error)

    result = runner.invoke(app, [], input=_cmds("q"))

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "could not close the board connection: close failed" in result.stderr
    close.assert_called_once_with()


def test_regression_testdrive_close_error_preserves_primary_hardware_diagnostic(
    fake_arm, mocker
):
    mocker.patch.object(fake_arm, "jog", side_effect=DeviceTimeout("primary timeout"))
    close = mocker.patch.object(fake_arm, "close", side_effect=OSError("close failed"))

    result = runner.invoke(app, [], input=_cmds("x1"))

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "primary timeout" in result.stderr
    assert "could not close the board connection: close failed" in result.stderr
    assert result.stderr.index("primary timeout") < result.stderr.index("close failed")
    close.assert_called_once_with()


@pytest.mark.parametrize(
    ("error", "exit_code"), [(KeyboardInterrupt(), 130), (SystemExit(7), 7)]
)
def test_regression_testdrive_close_error_preserves_cancellation(
    fake_arm, mocker, error, exit_code
):
    mocker.patch.object(fake_arm, "jog", side_effect=error)
    close = mocker.patch.object(fake_arm, "close", side_effect=OSError("close failed"))

    result = runner.invoke(app, [], input=_cmds("x1"))

    assert result.exit_code == exit_code
    assert "could not close the board connection: close failed" in result.stderr
    close.assert_called_once_with()


def test_regression_testdrive_close_error_does_not_mask_unexpected_exception(
    fake_arm, mocker
):
    error = ValueError("unexpected primary failure")
    mocker.patch.object(fake_arm, "jog", side_effect=error)
    close = mocker.patch.object(fake_arm, "close", side_effect=OSError("close failed"))

    result = runner.invoke(app, [], input=_cmds("x1"))

    assert result.exception is error
    assert "could not close the board connection: close failed" in result.stderr
    close.assert_called_once_with()


def _invoke_from_error_handler():
    try:
        raise ValueError("unrelated caller error")
    except ValueError:
        return runner.invoke(app, [], input=_cmds("q"))


def test_testdrive_close_failure_remains_nonzero_inside_caller_error_handler(
    fake_arm, mocker
):
    mocker.patch.object(fake_arm, "close", side_effect=OSError("close failed"))

    result = _invoke_from_error_handler()

    assert result.exit_code == 1
    assert "could not close the board connection: close failed" in result.stderr


@pytest.mark.parametrize("termination", ["q", "quit", "exit", "Q", " QuIt "])
def test_testdrive_quit_aliases_stop_before_next_move(fake_arm, termination):
    commands = _cmds(termination, "x10")

    result = runner.invoke(app, [], input=commands)

    assert result.exit_code == 0
    assert fake_arm.jogs == []
    assert fake_arm.closed


@pytest.mark.parametrize("error", [EOFError(), KeyboardInterrupt()])
def test_testdrive_prompt_eof_or_interrupt_finishes_cleanly(fake_arm, mocker, error):
    prompt = mocker.patch(
        "builtins.input",
        side_effect=[error, AssertionError("prompt read after termination")],
    )

    result = runner.invoke(app, [])

    assert result.exit_code == 0
    assert fake_arm.closed
    assert fake_arm.jogs == []
    prompt.assert_called_once_with("testdrive> ")


@pytest.mark.parametrize("command", ["tap", "longpress", "wat", "x999", ""])
def test_testdrive_commands_keep_prompt_open_for_next_move(fake_arm, command):
    commands = _cmds(command, "x1", "q")

    result = runner.invoke(app, [], input=commands)

    assert result.exit_code == 0
    assert fake_arm.jogs == [(1.0, 0.0)]
    assert fake_arm.closed


@pytest.mark.parametrize("operation", ["setup", "jog", "tap", "long_press"])
def test_testdrive_interrupt_during_hardware_operation_closes_once(
    fake_arm, mocker, operation
):
    mocker.patch.object(fake_arm, operation, side_effect=KeyboardInterrupt())
    close = mocker.spy(fake_arm, "close")

    result = runner.invoke(app, [], input=_cmds("x1", "tap", "longpress", "q"))

    assert result.exit_code == 130
    close.assert_called_once_with()


def test_testdrive_without_readline_still_accepts_commands(fake_arm, mocker):
    mocker.patch.dict(sys.modules, {"readline": None})

    result = runner.invoke(app, [], input=_cmds("x1", "q"))

    assert result.exit_code == 0
    assert fake_arm.jogs == [(1.0, 0.0)]
    assert fake_arm.closed


@pytest.mark.parametrize(
    "command",
    [
        "",
        "x",
        "y",
        "xnan",
        "yinf",
        "x1e2",
        "x+1",
        "x--1",
        "y1x1",
        "x1x2",
        "x1junk",
        "G0 X1",
        "x1;M3",
    ],
)
def test_testdrive_malformed_moves_never_reach_hardware(fake_arm, command):
    commands = _cmds(command, "q")

    result = runner.invoke(app, [], input=commands)

    assert result.exit_code == 0
    assert "try X10, Y10, X-10Y-10" in result.output
    assert fake_arm.jogs == []
    assert fake_arm.taps == 0
    assert fake_arm.long_presses == 0


@settings(max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(x=st.integers(-200, 200), y=st.integers(-500, 500))
def test_testdrive_valid_decimal_moves_preserve_signed_distances(mocker, x, y):
    arm = FakeArm()
    mocker.patch("physiclaw.core.hardware.arm.StylusArm", return_value=arm)
    mocker.patch("physiclaw.common.runtime_state.read_live", return_value=None)
    commands = _cmds(f"X{x / 10} Y{y / 10}", "q")

    result = runner.invoke(app, [], input=commands)

    assert result.exit_code == 0
    assert arm.jogs == [(x / 10, y / 10)]
    assert arm.closed


@settings(max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    axis=st.sampled_from(["x", "y"]),
    sign=st.sampled_from([-1, 1]),
    excess=st.integers(1, 10000),
)
def test_testdrive_out_of_range_moves_never_reach_hardware(mocker, axis, sign, excess):
    arm = FakeArm()
    mocker.patch("physiclaw.core.hardware.arm.StylusArm", return_value=arm)
    mocker.patch("physiclaw.common.runtime_state.read_live", return_value=None)
    cap = {"x": 20, "y": 50}[axis]
    commands = _cmds(f"{axis}{sign * (cap + excess / 10)}", "q")

    result = runner.invoke(app, [], input=commands)

    assert result.exit_code == 0
    assert "within ±20 mm in X and ±50 mm in Y" in result.output
    assert arm.jogs == []
    assert arm.closed
