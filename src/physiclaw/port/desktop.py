"""Native desktop operations; callers own file contents and validation."""

import shutil
import subprocess
from pathlib import Path

from physiclaw.port._select import native as _native


def edit_file(path: Path, *, editor: str | None = None) -> None:
    """Wait for an editor without a shell, preserving native command quoting."""
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
        command = [candidate, str(path)]
    elif not editor.strip():
        raise ValueError("editor command is empty")
    elif Path(editor).is_file():
        # A bare executable path may contain spaces or backslashes.
        command = [editor, str(path)]
    else:
        command = _native.editor_command(editor, str(path))
    subprocess.run(command, check=True)
