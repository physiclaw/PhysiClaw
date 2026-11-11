"""Native filesystem details behind the application's portable text policy."""

import errno
from pathlib import Path

from physiclaw.port._select import native as _native


def resolve_path(path: Path, *, allow_missing: bool = False) -> Path:
    """Resolve symlinks without hiding cycles on Python 3.13 and later."""
    try:
        return path.resolve(strict=True)
    except FileNotFoundError:
        if allow_missing:
            return path.resolve()
        raise
    except OSError as error:
        # Windows ERROR_CANT_RESOLVE_FILENAME also identifies a symlink cycle.
        if error.errno == errno.ELOOP or getattr(error, "winerror", None) == 1921:
            raise RuntimeError(f"Symlink loop from {path}") from error
        raise


def native_path(path: Path) -> Path:
    """Prepare an absolute local path for native I/O, preserving UNC paths."""
    return _native.native_path(path)


def replace_file(source: Path, target: Path) -> None:
    """Replace a sibling file, handling bounded native sharing contention."""
    _native.replace_file(source, target)
