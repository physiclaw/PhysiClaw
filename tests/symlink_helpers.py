"""Create real test links; allow missing local Windows privileges, but require CI."""

import os

import pytest


def symlink_to(path, target, target_is_directory=False):
    try:
        path.symlink_to(target, target_is_directory=target_is_directory)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314 and not os.environ.get(
            "GITHUB_ACTIONS"
        ):
            pytest.skip(
                "Creating real symlinks requires Windows Developer Mode or symlink privilege"
            )
        raise
