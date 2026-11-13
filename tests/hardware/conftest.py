"""Isolated hardware source copies for subprocess tests."""

import shutil

import pytest

from hardware.scheme import REPO_ROOT


@pytest.fixture
def source_tree(tmp_path):
    shutil.copytree(
        REPO_ROOT / "hardware",
        tmp_path / "hardware",
        ignore=shutil.ignore_patterns("output", "__pycache__"),
    )
    return tmp_path
