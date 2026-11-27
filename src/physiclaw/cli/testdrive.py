"""``physiclaw testdrive`` — take the board for a spin after flashing.

The post-flash bring-up check accepts ``X10`` / ``X-10Y-10`` style moves
plus ``tap`` and ``longpress`` as a single argument or at an interactive
prompt. Every command routes through ``StylusArm``, so moves are
idle-synced and the solenoid keeps its safety profile.
(``scripts/grbl_jog.py`` keeps scripted per-motor phases for deeper
diagnosis.)

Runs alongside the firmware guide, which explains the machine's axes
and wiring. The terminal focuses on controls and errors.
"""

from __future__ import annotations

import logging
import re
import sys
from contextlib import ExitStack
from typing import TYPE_CHECKING, Annotated, Literal

import typer

from physiclaw.cli._format import _style, exit_error, info, ok, warn
from physiclaw.common.terminal import color_enabled, is_terminal, stdout_text

if TYPE_CHECKING:
    from prompt_toolkit import PromptSession

    from physiclaw.core.hardware.arm import StylusArm

type _Action = Literal["tap", "longpress"] | tuple[float, float]

# Per-command caps (inclusive): no homing or limit switches, so any one
# move stays small enough that a typo can't run the carriage hard into
# the frame. X is the short axis — the top beam's usable travel is well
# under Y's — so its cap is tighter.
_MAX_JOG_X_MM = 20
_MAX_JOG_Y_MM = 50

# `X20`, `Y-10`, `X1.5 Y-2.5` — case normalized before parsing.
_MOVE_RE = re.compile(r"(?:x(-?\d+(?:\.\d+)?))?[ \t]*(?:y(-?\d+(?:\.\d+)?))?")


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


def _parse_action(cmd: str) -> _Action:
    match cmd:
        case "tap" | "longpress" as action:
            return action
    move = _MOVE_RE.fullmatch(cmd)
    if not cmd or move is None:
        raise ValueError("Unknown command.")
    dx, dy = map(float, move.groups("0"))
    if abs(dx) > _MAX_JOG_X_MM or abs(dy) > _MAX_JOG_Y_MM:
        raise ValueError(
            f"Move skipped. Max: X {_MAX_JOG_X_MM} mm / "
            f"Y {_MAX_JOG_Y_MM} mm, either direction."
        )
    return dx, dy


def _run_action(arm: StylusArm, action: _Action) -> bool:
    if action == "tap":
        arm.tap()
    elif action == "longpress":
        arm.long_press()
    elif action == (0, 0):
        typer.echo(info("No movement requested (0 mm)."))
        return False
    else:
        arm.jog(*action)
    return True


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


def _run_session(arm: StylusArm) -> None:
    # The plain-input fallback gets history through readline on POSIX.
    # Colored sessions use prompt_toolkit's editor on every platform.
    try:
        import readline  # noqa: F401
    except ImportError:
        pass

    _show_controls()
    session = _prompt_session()
    last_result: bool | None = None
    while True:
        # EOF and Ctrl-C at the prompt end the session like q.
        try:
            raw = _read_command(last_result, session=session)
        except (EOFError, KeyboardInterrupt):
            if session is None:
                typer.echo()
            return
        cmd = raw.strip().lower()
        if not cmd:
            continue
        if cmd in ("q", "quit", "exit"):
            return
        if cmd in ("help", "?"):
            _show_controls()
            last_result = None
            continue
        try:
            action = _parse_action(cmd)
        except ValueError as e:
            typer.echo("  " + warn(str(e)))
            last_result = False
        else:
            last_result = True if _run_action(arm, action) else None


def testdrive(
    port: Annotated[
        str | None,
        typer.Option("--port", help="Serial port (default: auto-detect)."),
    ] = None,
    command: Annotated[
        str | None,
        typer.Argument(help="Run one action and exit (e.g. x10, tap, longpress)."),
    ] = None,
) -> None:
    """Take the board for a spin after flashing or re-wiring.

    Keep the phone off the bed. Omit COMMAND to open an interactive session.
    """
    action = None
    if command is not None:
        command = command.strip().lower()
        try:
            action = _parse_action(command)
        except ValueError as e:
            exit_error(str(e), code=2)

    from physiclaw.common import paths, runtime_state
    from physiclaw.port.locks import FileLockBusy, exclusive_file_lock

    live = runtime_state.read_live()
    if live:
        exit_error(
            f"physiclaw server is running (pid {live.get('pid')}) and owns "
            "the serial port — stop it first, then re-run testdrive."
        )

    # The server takes this lock before publishing its PID and keeps it during
    # shutdown. Hold it through serial cleanup so test-drive sessions also
    # exclude one another.
    with ExitStack() as resources:
        try:
            resources.enter_context(
                exclusive_file_lock(paths.runtime_state_file().with_suffix(".lock"))
            )
        except FileLockBusy:
            exit_error("Another PhysiClaw session is running or starting.")
        except OSError as e:
            exit_error(f"Cannot acquire the PhysiClaw runtime lock: {e}")
        performed_action = _drive_board(port, action)

    if performed_action:
        typer.echo("  " + ok(f"Completed {command}."))


def _drive_board(port: str | None, action: _Action | None) -> bool:
    # Lazy: importing StylusArm runs the core.hardware package __init__,
    # which pulls in the camera stack (cv2) — too heavy for CLI import
    # time. Same rule as `flash` and `server`.
    from physiclaw.core.hardware.arm import StylusArm
    from physiclaw.core.hardware.device import HardwareError

    typer.echo(_style("Test drive", bold=True))
    typer.echo(info("Connecting to the control board..."))
    try:
        arm = StylusArm(port)
    except (HardwareError, OSError) as e:
        exit_error(f"Could not connect to the control board: {e}")

    performed_action = None  # Set only after the session or action completes.
    try:
        arm.setup()
        typer.echo("  " + ok(f"Connected on {arm.port}"))
        if action is None:
            _run_session(arm)
            performed_action = False
        else:
            performed_action = _run_action(arm, action)
        return performed_action
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
            if performed_action is not None:
                exit_error(message)
            from physiclaw.common.log_style import report_error

            report_error(message)
