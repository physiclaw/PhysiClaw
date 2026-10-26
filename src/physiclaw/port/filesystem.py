"""Native filesystem details behind the application's portable text policy."""

from pathlib import Path

from physiclaw.port._select import native as _native


def native_path(path: Path) -> Path:
    """Prepare an absolute local path for native I/O, preserving UNC paths."""
    return _native.native_path(path)


def replace_file(source: Path, target: Path) -> None:
    """Replace a sibling file, handling bounded native sharing contention."""
    _native.replace_file(source, target)
