"""Archive subprocess logs without traversing pytest's symlink fixtures."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def collect_logs(root: Path) -> None:
    if not root.is_dir():
        return
    with ZipFile(
        root / "subprocess-logs.zip", "w", ZIP_DEFLATED, strict_timestamps=False
    ) as archive:
        for directory, directories, files in root.walk(follow_symlinks=False):
            # Windows junctions also redirect directory traversal.
            directories[:] = [
                name for name in directories if not (directory / name).is_junction()
            ]
            for name in files:
                path = directory / name
                if path.suffix == ".log" and not path.is_symlink() and path.is_file():
                    archive.write(path, path.relative_to(root))


if __name__ == "__main__":
    collect_logs(Path(".test-results"))
