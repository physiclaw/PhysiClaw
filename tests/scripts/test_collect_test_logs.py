"""Collect diagnostic logs even when test fixtures contain broken links."""

import os
import subprocess
from zipfile import ZipFile

import pytest

from scripts.collect_test_logs import collect_logs


@pytest.mark.parametrize("timestamp", [None, 1])
def test_collects_nested_logs_without_other_test_data(tmp_path, timestamp):
    logs = tmp_path / "tmp-integration" / "test_shutdown"
    logs.mkdir(parents=True)
    content = "shutdown café 校准\n".encode()
    (logs / "process.log").write_bytes(content)
    if timestamp is not None:
        # Retention tests deliberately leave files dated before ZIP's 1980 floor.
        os.utime(logs / "process.log", (timestamp, timestamp))
    (logs / "config.toml").write_text("private", encoding="utf-8")
    (tmp_path / "fast.xml").write_text("report", encoding="utf-8")

    collect_logs(tmp_path)

    with ZipFile(tmp_path / "subprocess-logs.zip") as archive:
        assert archive.namelist() == ["tmp-integration/test_shutdown/process.log"]
        assert archive.read(archive.namelist()[0]) == content


def test_skips_symlink_loops_broken_links_and_external_logs(tmp_path):
    root = tmp_path / "reports"
    root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "private.log").write_text("private", encoding="utf-8")
    try:
        (root / "loop").symlink_to("loop")
        (root / "loop.log").symlink_to("loop.log")
        (root / "broken.log").symlink_to("missing")
        (root / "linked.log").symlink_to(external / "private.log")
        (root / "directory").symlink_to(external, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Symlinks unavailable: {error}")
    (root / "process.log").write_text("shutdown", encoding="utf-8")

    collect_logs(root)

    with ZipFile(root / "subprocess-logs.zip") as archive:
        assert archive.namelist() == ["process.log"]
        assert archive.read("process.log") == b"shutdown"


def test_missing_reports_after_setup_failure_are_allowed(tmp_path):
    collect_logs(tmp_path / "missing")
    assert not (tmp_path / "missing").exists()


@pytest.mark.skipif(os.name != "nt", reason="Native Windows directory junctions")
def test_skips_windows_junctions(tmp_path):
    root = tmp_path / "reports"
    root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "private.log").write_text("private", encoding="utf-8")
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(root / "junction"), str(external)],
        check=True,
        capture_output=True,
    )

    collect_logs(root)

    with ZipFile(root / "subprocess-logs.zip") as archive:
        assert archive.namelist() == []
