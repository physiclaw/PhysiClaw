"""Native directory links; callers choose output locations and cache policy."""

from pathlib import Path

from hardware.port._select import native as _native


def link_directory(source: Path, target: Path) -> None:
    _native.link_directory(source.resolve(), target)
