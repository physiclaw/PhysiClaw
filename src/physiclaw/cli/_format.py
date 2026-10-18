"""Shared output formatters for ``physiclaw`` CLI commands.

Every command uses the same ``✓ ok`` / ``! warn`` line shape and the same
"Next: <cmd>" footer; centralizing here keeps tone uniform and avoids
ad-hoc ``typer.style(...fg=...)`` repetition.

Two voices live here: the flat ``ok``/``warn``/``info`` lines most
commands echo, and the indented ``step_*`` lines of the setup wizard
(``physiclaw setup hardware``), which mirror the browser wizard's step
markers. ``exit_error`` is the one failure shape for expected errors.
"""

import sys
from typing import NoReturn

import typer

from physiclaw.common.terminal import color_enabled, stdout_text


def _style(text: str, *, fg: str, bold: bool = False) -> str:
    text = stdout_text(text)
    return typer.style(text, fg=fg, bold=bold) if color_enabled(sys.stdout) else text


def ok(msg: str) -> str:
    """Green ``✓`` prefix + msg."""
    return _style("✓ ", fg=typer.colors.GREEN) + stdout_text(msg)


def warn(msg: str) -> str:
    """Yellow ``!`` prefix + msg."""
    return _style("! ", fg=typer.colors.YELLOW) + stdout_text(msg)


def next_hint(line: str) -> str:
    """Bold ``Next:`` prefix + the rest of the line."""
    return _style("Next:", fg=typer.colors.CYAN, bold=True) + " " + stdout_text(line)


def info(msg: str) -> str:
    """Two-space indent + msg. Use for neutral state lines that shouldn't
    read as a problem (unlike ``warn``) or a confirmation (unlike ``ok``)."""
    return f"  {stdout_text(msg)}"


def section(title: str) -> str:
    """Bold cyan section header. Distinct from ok (green) and warn
    (yellow) so the reader can scan section boundaries at a glance in
    commands that emit multi-section reports (``doctor``, ``status``)."""
    return _style(title, fg=typer.colors.CYAN, bold=True)


def step_ok(msg: str = "OK") -> str:
    """Setup-wizard step line: two-space indent + green ``✓`` + msg."""
    return f"  {_style('✓', fg=typer.colors.GREEN)} {stdout_text(msg)}"


def step_fail(msg: str) -> str:
    """Setup-wizard step failure: two-space indent + red ``✗ msg``."""
    return "  " + _style(f"✗ {msg}", fg=typer.colors.RED)


def step_warn(msg: str) -> str:
    """Setup-wizard step warning: two-space indent + yellow ``⚠ msg``."""
    return "  " + _style(f"⚠ {msg}", fg=typer.colors.YELLOW)


def exit_error(msg: str, code: int = 1) -> NoReturn:
    """Log one expected error to stderr and exit with ``code``.

    Uses the shared pretty/JSON format without a traceback for bad input,
    network failures, corrupt downloads or missing files.
    """
    from physiclaw.common.log_style import report_error

    report_error(msg, exit_code=code)
    raise typer.Exit(code=code)


def parse_inputs(pairs: list[str], flag: str = "--input") -> dict[str, str]:
    """Repeatable ``NAME=VALUE`` pairs → dict — the one parser for every
    command that feeds declared values (`macros rehearse`, `playbooks
    arm`, install's ``--set``). Exits code 2 on a malformed pair."""
    values: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            exit_error(f"bad {flag} {pair!r} (want key=value)", code=2)
        values[key] = value
    return values


def state_tag(*, valid: bool, enabled: bool = False) -> str:
    """The colored invalid/enabled/disabled tag both artifact listings
    (`macros list`, `playbooks list`) render — one palette, one wording."""
    if not valid:
        return _style("invalid ", fg=typer.colors.RED)
    if enabled:
        return _style("enabled ", fg=typer.colors.GREEN)
    return _style("disabled", fg=typer.colors.YELLOW)
