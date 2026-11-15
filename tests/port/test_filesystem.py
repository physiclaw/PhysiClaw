"""Real tree removal, including Windows read-only file attributes."""

import os
import stat

import pytest

from physiclaw.port.filesystem import remove_tree


def test_remove_tree_removes_readonly_files(tmp_path):
    root = tmp_path / "owned"
    root.mkdir()
    path = root / "readonly.txt"
    path.write_bytes(b"readonly")
    path.chmod(stat.S_IREAD)
    try:
        remove_tree(root)
        assert not root.exists()
    finally:
        if path.exists():
            path.chmod(stat.S_IWRITE)


def test_remove_tree_reports_missing_tree(tmp_path):
    with pytest.raises(FileNotFoundError):
        remove_tree(tmp_path / "missing")


def test_posix_cleanup_does_not_hide_permission_errors():
    from physiclaw.port.backends import posix

    error = PermissionError("denied")
    with pytest.raises(PermissionError, match="denied"):
        posix.remove_readonly(os.unlink, "unused", error)
