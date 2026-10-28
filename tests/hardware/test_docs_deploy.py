"""Exercise the real docs hook against a local HTTP server on every OS."""

import subprocess
import sys
from http.client import BadStatusLine, InvalidURL
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from hardware.port.processes import owned_process
from hardware.scheme import REPO_ROOT
from scripts import docs_deploy


@pytest.fixture
def docs_hook(tmp_path, monkeypatch, status):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append((self.command, self.path))
            self.send_response(status)
            self.send_header("Location", "/redirect")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            self.do_POST()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        hook = f"http://127.0.0.1:{server.server_port}/secret"
        (tmp_path / ".docs-deploy-hook").write_text(hook, encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        # urllib also consults OS proxy settings; keep loopback requests local.
        monkeypatch.setenv("NO_PROXY", "*")
        monkeypatch.setenv("no_proxy", "*")
        yield hook, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("status", [200, 202, 302, 404, 500])
def test_docs_hook_requires_http_success(status, docs_hook, capsys):
    _, requests = docs_hook

    returncode = docs_deploy.main()

    output = capsys.readouterr()
    assert returncode == (0 if 200 <= status < 300 else 1)
    if 200 <= status < 300:
        assert output.out.strip() == "docs-site deploy triggered"
        assert output.err == ""
    else:
        assert output.out == ""
        assert output.err.strip() == f"Docs deploy hook failed: HTTP {status}"
    assert requests == [("POST", "/secret")]


def run_docs_command(tmp_path, argv):
    # Startup time varies on hosted runners. This is a hang guard, not a
    # performance assertion; own the tree so a timed-out make cannot leak Python.
    with owned_process(
        argv,
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as owner:
        process = owner.process
        try:
            stdout, stderr = process.communicate(timeout=60)
        except subprocess.TimeoutExpired as exc:
            owner.stop(timeout=0)
            stdout, stderr = process.communicate(timeout=5)
            pytest.fail(f"{exc}\nstdout:\n{stdout}\nstderr:\n{stderr}")
        return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


@pytest.mark.parametrize("status", [200, 500])
def test_make_docs_deploy_propagates_result(tmp_path, status, docs_hook):
    hook, requests = docs_hook
    result = run_docs_command(
        tmp_path,
        [
            "make",
            "--no-print-directory",
            "-f",
            str(REPO_ROOT / "Makefile"),
            "docs-deploy",
            f"PYTHON={Path(sys.executable).as_posix()}",
        ],
    )

    output = result.stdout + result.stderr
    assert (result.returncode == 0) is (status == 200), output
    if status == 200:
        assert "docs-site deploy triggered" in result.stdout, output
        assert result.stderr == "", output
    else:
        assert "Docs deploy hook failed: HTTP 500" in result.stderr, output
        assert "docs-site deploy triggered" not in result.stdout, output
    assert requests == [("POST", "/secret")], output
    assert hook not in output


def test_docs_hook_missing_configuration_fails(tmp_path):
    result = run_docs_command(
        tmp_path,
        [sys.executable, str(REPO_ROOT / "scripts/docs_deploy.py")],
    )

    assert result.returncode == 1
    assert ".docs-deploy-hook missing" in result.stderr


def test_malformed_hook_does_not_disclose_secret_in_traceback(tmp_path):
    hook = "http://127.0.0.1/secret token"
    (tmp_path / ".docs-deploy-hook").write_text(hook, encoding="utf-8")

    result = run_docs_command(
        tmp_path,
        [sys.executable, str(REPO_ROOT / "scripts/docs_deploy.py")],
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
