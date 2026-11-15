"""Native desktop operations; callers own file contents and validation."""

import shutil
from pathlib import Path

from physiclaw.port._select import native as _native


def edit_file(path: Path, *, editor: str | None = None) -> None:
    """Select an editor and wait for its native launcher to finish."""
    command: list[str] | str
    if not editor:
        candidate = next(
            (name for name in _native.EDITOR_CANDIDATES if shutil.which(name)), None
        )
        if candidate is None:
            choices = "/".join(_native.EDITOR_CANDIDATES)
            raise FileNotFoundError(
                f"no $EDITOR set and no {choices} on PATH. Edit by hand: {path}"
            )
        command = [candidate]
    elif not editor.strip():
        raise ValueError("editor command is empty")
    elif Path(editor).is_file():
        # A bare executable path may contain spaces or backslashes.
        command = [editor]
    else:
        command = editor
    _native.run_editor(command, str(path))
