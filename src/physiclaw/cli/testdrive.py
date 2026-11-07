"""``physiclaw testdrive`` — take the board for a spin after flashing.

The post-flash bring-up check: connect to the board, then hand the user
the controls — ``X10`` / ``X-10Y-10`` style
moves plus ``tap`` and ``longpress`` — so they watch the motors and
solenoid do what they typed. No pass/fail bookkeeping: the user's own
eyes are the verdict. Every command routes through ``StylusArm``, so
moves are idle-synced and the solenoid keeps its safety profile.
(``scripts/grbl_jog.py`` keeps scripted per-motor phases for deeper
diagnosis.)

Runs alongside the firmware guide, which explains the machine's axes
and wiring. The terminal focuses on controls and errors.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import TYPE_CHECKING, Annotated

import typer

from physiclaw.cli._format import _style, exit_error, info, ok, warn
from physiclaw.common.terminal import color_enabled, is_terminal, stdout_text

if TYPE_CHECKING:
    from prompt_toolkit import PromptSession

# Per-command caps (inclusive): no homing or limit switches, so any one
# move stays small enough that a typo can't run the carriage hard into
# the frame. X is the short axis — the top beam's usable travel is well
# under Y's — so its cap is tighter.
_MAX_JOG_X_MM = 20
_MAX_JOG_Y_MM = 50

# `X20`, `Y-10`, `X1.5Y-2.5` — case and spaces already normalized away.
_MOVE_RE = re.compile(r"^(?:x(?P<x>-?\d+(?:\.\d+)?))?(?:y(?P<y>-?\d+(?:\.\d+)?))?$")


def _show_controls() -> None:
    """A compact reference; the firmware guide owns diagrams and wiring."""
    typer.echo()
    typer.echo(_style("Controls", bold=True))
    typer.echo("  " + _style("Type a command, then press Enter.", dim=True))
    for commands in (
        (
            ("X10", "Move X by +10 mm"),
            ("Y10", "Move Y by +10 mm"),
            ("X-10Y-10", "Move X by -10 mm and Y by -10 mm"),
        ),
        (
            ("tap", "Tap once"),
            ("longpress", "Press and hold the stylus"),
        ),
        (
            ("help", "Show controls"),
            ("q", "End test drive"),
        ),
    ):
        typer.echo()
        for command, description in commands:
            label = _style(f"{command:<9}", fg=typer.colors.CYAN)
            typer.echo(f"  {label}  {stdout_text(description)}")
    typer.echo()
    typer.echo(
        info(
            f"Max per move: X {_MAX_JOG_X_MM} mm / Y {_MAX_JOG_Y_MM} mm, "
            "either direction."
        )
    )
    typer.echo()


def _parse_move(cmd: str) -> tuple[float, float] | None:
    """(dx, dy) mm for a move command, or None if it isn't one."""
    m = _MOVE_RE.fullmatch(cmd)
    if not m or (m.group("x") is None and m.group("y") is None):
        return None
    return float(m.group("x") or 0), float(m.group("y") or 0)


def _prompt_session() -> PromptSession[str] | None:
    """Use a portable line editor for color; retain plain input for pipes."""
    if not is_terminal(sys.stdin) or not color_enabled(sys.stdout):
        return None
    try:
        from prompt_toolkit import PromptSession

        return PromptSession()
    except Exception:
        # Some emulated consoles report a TTY but lack native handles.
        # An optional display enhancement must not block the command loop.
        logging.getLogger(__name__).debug(
            "Interactive prompt unavailable; using plain input", exc_info=True
        )
        return None


def _read_command(result: bool | None, *, session: PromptSession[str] | None) -> str:
    """Style only the arrow; the label, input and line editing stay neutral."""
    marker = stdout_text("→")
    if session is None:
        return input(f"testdrive {marker} ")
    style = "bold"
    if result is not None:
        style += " ansigreen" if result else " ansired"
    return session.prompt([("", "testdrive "), (style, marker), ("", " ")])


def testdrive(
    port: Annotated[
        str | None,
        typer.Option("--port", help="Serial port (default: auto-detect)."),
    ] = None,
) -> None:
    """Take the board for a spin — drive the motors and solenoid yourself.

    Run after flashing or re-wiring, with the phone off the bed.
    Type `help` during the session to see the controls.
    """
    from physiclaw.common import runtime_state

    live = runtime_state.read_live()
    if live:
        exit_error(
            f"physiclaw server is running (pid {live.get('pid')}) and owns "
            "the serial port — stop it first, then re-run testdrive."
        )

    # Lazy: importing StylusArm runs the core.hardware package __init__,
    # which pulls in the camera stack (cv2) — too heavy for CLI import
    # time. Same rule as `flash` and `server`.
    from physiclaw.core.hardware.arm import StylusArm
    from physiclaw.core.hardware.device import HardwareError

    # The plain-input fallback gets history through readline on POSIX.
    # Colored sessions use prompt_toolkit's editor on every platform.
    # Windows does not provide stdlib readline, so keep this optional.
    try:
        import readline  # noqa: F401
    except ImportError:
        pass

    typer.echo(_style("Test drive", bold=True))
    typer.echo(info("Connecting to the control board..."))
    try:
        arm = StylusArm(port)
    except (HardwareError, OSError) as e:
        exit_error(f"Could not connect to the control board: {e}")

    completed = False
    try:
        arm.setup()
        typer.echo("  " + ok(f"Connected on {arm.port}"))
        _show_controls()

        session = _prompt_session()
        last_result: bool | None = None
        while True:
            # EOF and Ctrl-C mean "done", with the same board cleanup as q.
            try:
                raw = _read_command(last_result, session=session)
            except (EOFError, KeyboardInterrupt):
                if session is None:
                    typer.echo()
                break
            cmd = "".join(raw.split()).lower()
            if not cmd:
                continue
            if cmd in ("q", "quit", "exit"):
                break
            if cmd in ("help", "?"):
                _show_controls()
                last_result = None
                continue
            if cmd == "tap":
                arm.tap()
                last_result = True
                continue
            if cmd == "longpress":
                arm.long_press()
                last_result = True
                continue
            move = _parse_move(cmd)
            if move is None:
                last_result = False
                typer.echo("  " + warn("Unknown command."))
                continue
            dx, dy = move
            if abs(dx) > _MAX_JOG_X_MM or abs(dy) > _MAX_JOG_Y_MM:
                last_result = False
                typer.echo(
                    "  "
                    + warn(
                        f"Move skipped. Max: X {_MAX_JOG_X_MM} mm / "
                        f"Y {_MAX_JOG_Y_MM} mm, either direction."
                    )
                )
                continue
            if dx == 0 and dy == 0:
                typer.echo(info("No movement requested (0 mm)."))
                last_result = None
                continue
            arm.jog(dx, dy)
            last_result = True
        completed = True
    except (HardwareError, OSError) as e:
        # Board answered at connect but died mid-run (cable yanked, alarm,
        # serial silence) — a plain exit, not a traceback.
        exit_error(f"Test drive stopped: {e}")
    finally:
        # Always attempt release; a cleanup failure must not replace an
        # operation's exception or cancellation already leaving the try block.
        try:
            arm.close()
        except (HardwareError, OSError) as e:
            message = f"could not close the board connection: {e}"
            if completed:
                exit_error(message)
            from physiclaw.common.log_style import report_error

            report_error(message)
