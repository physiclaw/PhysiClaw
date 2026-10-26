"""Create real test links, with an explicit capability skip on Windows."""

import pytest


def symlink_to(path, target, target_is_directory=False):
    try:
        path.symlink_to(target, target_is_directory=target_is_directory)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip(
                "Creating real symlinks requires Windows Developer Mode or symlink privilege"
            )
        raise
