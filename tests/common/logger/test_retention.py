"""Retention failures must preserve evidence without stopping healthy cleanup."""

import os
from pathlib import Path

import pytest
from freezegun import freeze_time

from physiclaw.common.logger import retention


def _old_file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("session evidence", encoding="utf-8")
    os.utime(path, (1, 1))
    return path


@pytest.mark.parametrize("failure", [PermissionError, FileNotFoundError])
def test_archive_enumeration_failure_returns_zero(tmp_path, mocker, failure):
    mocker.patch.object(Path, "glob", side_effect=failure("unavailable"))

    removed = retention.purge_log_archives(tmp_path, "runtime", days=30, backup_count=5)

    assert removed == 0


@pytest.mark.parametrize("operation", ["stat", "unlink"])
def test_archive_failure_preserves_evidence_and_cleans_other_files(
    tmp_path, monkeypatch, operation
):
    blocked = _old_file(tmp_path / "runtime.log.1")
    healthy = _old_file(tmp_path / "runtime.log.2")
    original = getattr(Path, operation)

    def deny_blocked(path, *args, **kwargs):
        if path == blocked:
            raise PermissionError("archive is busy")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, operation, deny_blocked)

        removed = retention.purge_log_archives(
            tmp_path, "runtime", days=30, backup_count=5
        )

    assert removed == 1
    assert blocked.read_text(encoding="utf-8") == "session evidence"
    assert not healthy.exists()


@pytest.mark.parametrize("failure", [PermissionError, FileNotFoundError])
def test_session_enumeration_failure_returns_zero(tmp_path, mocker, failure):
    mocker.patch.object(Path, "iterdir", side_effect=failure("unavailable"))

    removed = retention.purge_old_sessions(tmp_path, days=30)

    assert removed == 0


def test_session_removal_failure_preserves_evidence_and_cleans_other_sessions(
    tmp_path, mocker
):
    blocked = _old_file(tmp_path / "blocked" / "events.jsonl")
    healthy = _old_file(tmp_path / "healthy" / "events.jsonl")
    original = retention.shutil.rmtree

    def deny_blocked(path, *args, **kwargs):
        if path == blocked.parent:
            raise PermissionError("session is busy")
        return original(path, *args, **kwargs)

    mocker.patch.object(retention.shutil, "rmtree", side_effect=deny_blocked)

    removed = retention.purge_old_sessions(tmp_path, days=30)

    assert removed == 1
    assert blocked.read_text(encoding="utf-8") == "session evidence"
    assert not healthy.parent.exists()


@pytest.mark.parametrize("newest_age_days, removed", [(29, 0), (30, 0), (31, 1)])
@freeze_time("2026-10-30T12:00:00Z")
def test_session_retention_uses_newest_file_and_strict_age_boundary(
    tmp_path, newest_age_days, removed
):
    old = _old_file(tmp_path / "session" / "events.jsonl")
    newest = _old_file(old.parent / "screenshots" / "latest.jpg")
    modified = retention.time.time() - newest_age_days * 86400
    os.utime(newest, (modified, modified))

    actual = retention.purge_old_sessions(tmp_path, days=30)

    assert actual == removed
    assert newest.exists() == (removed == 0)
