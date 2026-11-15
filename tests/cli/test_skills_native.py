"""Native Git regressions; all writes use the isolated test home."""

import importlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

skills = importlib.import_module("physiclaw.cli.skills")
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.slow
def test_real_local_clone_is_removed_after_successful_install():
    if not (ROOT / ".git").exists():
        pytest.skip("requires a source checkout for a read-only local Git fixture")

    result = CliRunner().invoke(
        skills.skills_app, ["install", "jd", "--from", ROOT.as_uri()]
    )

    assert result.exit_code == 0, result.output
    home = skills.paths.skills_dir()
    assert (home / "jd" / "SKILL.md").is_file()
    assert not list(home.glob(".jd.installing-*"))
    assert "could not clean up" not in result.output


def test_git_utf8_output_with_python_utf8_mode_disabled(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "中文.txt").write_bytes(b"fixture")
    code = (
        "import json;from pathlib import Path;"
        "from physiclaw.cli.skills import _git;"
        f"result=_git('-c','core.quotePath=false','status','--porcelain',cwd=Path({str(tmp_path)!r}));"
        "print(json.dumps(result.stdout))"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8=0",
            "-X",
            "warn_default_encoding",
            "-W",
            "error::EncodingWarning",
            "-c",
            code,
        ],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        encoding="utf-8",
        check=True,
    )

    assert "中文.txt" in json.loads(result.stdout)
    assert "UnicodeDecodeError" not in result.stderr


def test_failed_local_clone_cleanup_preserves_source_object_permissions(tmp_path):
    source = tmp_path / "source.git"
    subprocess.run(["git", "init", "--bare", "-q", str(source)], check=True)
    result = subprocess.run(
        ["git", "--git-dir", str(source), "hash-object", "-w", "--stdin"],
        input=b"test-owned object",
        capture_output=True,
        check=True,
    )
    sha = result.stdout.decode("ascii").strip()
    object_path = source / "objects" / sha[:2] / sha[2:]
    permissions = stat.S_IMODE(object_path.stat().st_mode)

    # The clone owns copied objects even when the local transport is used.
    # Its missing HEAD deliberately exercises failed-install cleanup.
    result = CliRunner().invoke(
        skills.skills_app, ["install", "jd", "--from", str(source)]
    )

    assert result.exit_code == 1
    assert "git rev-parse failed" in result.output
    assert not list(skills.paths.skills_dir().glob(".jd.installing-*"))
    assert "could not clean up" not in result.output
    assert stat.S_IMODE(object_path.stat().st_mode) == permissions
