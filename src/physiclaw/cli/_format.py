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
