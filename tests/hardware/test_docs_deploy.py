"""Run the real docs-hook recipe with a fake transport; never send a request."""

from __future__ import annotations

import os
import subprocess

import pytest

from hardware.scheme import REPO_ROOT


@pytest.mark.parametrize(
    ("status", "transport_exit", "success"),
    [
        ("200", 0, True),
        ("202", 0, True),
        ("302", 0, False),
        ("404", 0, False),
        ("500", 0, False),
        ("000", 7, False),
    ],
)
def test_docs_hook_requires_http_success(tmp_path, status, transport_exit, success):
    (tmp_path / ".docs-deploy-hook").write_text("https://example.invalid/test-hook")
    curl = tmp_path / "curl"
    curl.write_text(f"#!/bin/sh\nprintf '%s' '{status}'\nexit {transport_exit}\n")
    curl.chmod(0o755)
    result = subprocess.run(
        [
            "make",
            "--no-print-directory",
            "-f",
            str(REPO_ROOT / "Makefile"),
            "docs-deploy",
        ],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert (result.returncode == 0) is success
    assert ("docs-site deploy triggered" in result.stdout) is success
