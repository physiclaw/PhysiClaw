"""Shared output formatters for ``physiclaw`` CLI commands.

Every command uses the same ``✓ ok`` / ``! warn`` line shape and the same
"Next: <cmd>" footer; centralizing here keeps tone uniform and avoids
ad-hoc ``typer.style(...fg=...)`` repetition.
"""

import typer


def ok(msg: str) -> str:
    """Green ``✓`` prefix + msg."""
    return typer.style("✓ ", fg=typer.colors.GREEN) + msg


def warn(msg: str) -> str:
    """Yellow ``!`` prefix + msg."""
    return typer.style("! ", fg=typer.colors.YELLOW) + msg


def next_hint(line: str) -> str:
    """Bold ``Next:`` prefix + the rest of the line."""
    return typer.style("Next:", bold=True) + " " + line


def info(msg: str) -> str:
    """Two-space indent + msg. Use for neutral state lines that shouldn't
    read as a problem (unlike ``warn``) or a confirmation (unlike ``ok``)."""
    return f"  {msg}"


def section(title: str) -> str:
    """Bold bright-cyan section header. Distinct from ok (green) and warn
    (yellow) so the reader can scan section boundaries at a glance in
    commands that emit multi-section reports (``doctor``, ``status``)."""
    return typer.style(title, fg=typer.colors.BRIGHT_CYAN, bold=True)
