"""Exercise the real docs hook against a local HTTP server on every OS."""

import os
import subprocess
import sys
from http.client import BadStatusLine, InvalidURL
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from hardware.scheme import REPO_ROOT
from scripts import docs_deploy


@pytest.mark.parametrize("status", [200, 202, 302, 404, 500])
def test_docs_hook_requires_http_success(tmp_path, status):
    methods = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            methods.append(self.command)
            self.send_response(status)
            self.send_header("Location", "/redirect")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    hook = f"http://127.0.0.1:{server.server_port}/secret"
    (tmp_path / ".docs-deploy-hook").write_text(hook, encoding="utf-8")
    try:
        result = subprocess.run(
            [
                "make",
                "--no-print-directory",
                "-f",
                str(REPO_ROOT / "Makefile"),
                "docs-deploy",
                f"PYTHON={Path(sys.executable).as_posix()}",
            ],
            cwd=tmp_path,
            env={**os.environ, "NO_PROXY": "127.0.0.1"},
            capture_output=True,
            text=True,
            timeout=10,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert (result.returncode == 0) is (200 <= status < 300)
    assert ("docs-site deploy triggered" in result.stdout) is (200 <= status < 300)
    assert methods == ["POST"]
    assert hook not in result.stdout + result.stderr


def test_docs_hook_missing_configuration_fails(tmp_path):
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts/docs_deploy.py")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 1
    assert ".docs-deploy-hook missing" in result.stderr


def test_malformed_hook_does_not_disclose_secret_in_traceback(tmp_path):
    hook = "http://127.0.0.1/secret token"
    (tmp_path / ".docs-deploy-hook").write_text(hook, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts/docs_deploy.py")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 1
    assert "Docs deploy hook request failed" in result.stderr
    assert "secret token" not in result.stdout + result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("error", [BadStatusLine, InvalidURL])
def test_http_protocol_errors_are_reported_without_secret(
    tmp_path, monkeypatch, mocker, capsys, error
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".docs-deploy-hook").write_text(
        "http://127.0.0.1/secret", encoding="utf-8"
    )
    mocker.patch.object(
        docs_deploy, "build_opener"
    ).return_value.open.side_effect = error("secret response")

    assert docs_deploy.main() == 1

    output = capsys.readouterr()
    assert output.err.strip() == "Docs deploy hook request failed"
    assert not output.out
