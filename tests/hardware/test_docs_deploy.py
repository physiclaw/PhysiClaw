"""Deployment ordering, bounded readiness, recovery, and secret-safe HTTP errors."""

import io
import json
import os
import subprocess
import sys
from http.client import BadStatusLine, InvalidURL
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import Mock

import pytest

from hardware.port.processes import owned_process
from hardware.scheme import REPO_ROOT
from scripts import docs_deploy

TAG = "physiclaw-hardware-v1.2"
DEPLOYMENT = "https://1234abcd.docs-site.pages.dev"
REPORTS = Path("hardware/output/site-asset-report")


@pytest.fixture
def status():
    return 200


@pytest.fixture
def docs_hook(tmp_path, monkeypatch, status):
    requests = []
    payload = {
        "success": True,
        "result": {"url": DEPLOYMENT, "environment": "production", "is_skipped": False},
    }

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append((self.command, self.path))
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Location", "/redirect")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

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
        monkeypatch.setenv("HW_VERSION", "1.2")
        # urllib also consults OS proxy settings; keep loopback requests local.
        monkeypatch.setenv("NO_PROXY", "*")
        monkeypatch.setenv("no_proxy", "*")
        yield hook, requests, payload
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("status", [200, 202, 302, 404, 500])
def test_docs_hook_posts_once_and_requires_http_success(status, docs_hook, capsys):
    hook, requests, _ = docs_hook
    if 200 <= status < 300:
        assert docs_deploy.trigger_hook(hook) == DEPLOYMENT
        assert capsys.readouterr().out.strip() == "docs-site deploy triggered"
    else:
        with pytest.raises(docs_deploy.DeployError, match=f"HTTP {status}") as error:
            docs_deploy.trigger_hook(hook)
        assert hook not in str(error.value)
        assert not capsys.readouterr().out
    assert requests == [("POST", "/secret")]


@pytest.mark.parametrize(
    "change",
    [
        {"success": False},
        {"result": None},
        {"result": {"url": "https://docs-site.pages.dev"}},
        {"result": {"url": "https://main.docs-site.pages.dev"}},
        {"result": {"url": "https://1234abcd.docs-site.pages.dev/secret"}},
        {"result": {"url": "https://1234abcd.docs-site.pages.dev.evil.example"}},
        {"result": {"url": DEPLOYMENT, "environment": "preview"}},
        {
            "result": {
                "url": DEPLOYMENT,
                "environment": "production",
                "is_skipped": True,
            }
        },
    ],
)
def test_hook_requires_a_fresh_production_deployment(docs_hook, change):
    hook, requests, payload = docs_hook
    payload.update(change)
    with pytest.raises(docs_deploy.DeployError):
        docs_deploy.trigger_hook(hook)
    assert len(requests) == 1


@pytest.fixture
def verification(monkeypatch):
    calls = Mock()
    monkeypatch.setattr(docs_deploy, "resolve_tag", calls.resolve)
    monkeypatch.setattr(docs_deploy, "wait_for_deployment", calls.wait)
    monkeypatch.setattr(docs_deploy, "audit_sites", calls.audit)
    calls.resolve.return_value = TAG
    calls.audit.return_value = 0
    return calls


def test_deploy_pins_before_hook_waits_then_audits_once(
    docs_hook, verification, tmp_path
):
    hook, requests, _ = docs_hook

    def wait(*args):
        assert requests == [("POST", "/secret")]
        verification.audit.assert_not_called()

    verification.wait.side_effect = wait
    assert docs_deploy.main([]) == 0
    assert [c[0] for c in verification.mock_calls] == ["resolve", "wait", "audit"]
    verification.resolve.assert_called_once_with(TAG)
    verification.wait.assert_called_once_with(DEPLOYMENT, TAG, 900, 15)
    directory = next((tmp_path / REPORTS).iterdir())
    verification.audit.assert_called_once_with(TAG, directory / "assets.csv")
    assert json.loads((directory / "deployment.json").read_text(encoding="utf-8")) == {
        "url": DEPLOYMENT,
        "hardware_tag": TAG,
    }
    log = (directory / "check.log").read_text(encoding="utf-8")
    assert "verification passed" in log
    assert hook not in log


@pytest.mark.parametrize("stage", ["hook", "wait", "audit", "interrupt"])
def test_failure_never_redeploys_or_claims_success(
    docs_hook, verification, tmp_path, stage
):
    hook, requests, payload = docs_hook
    if stage == "hook":
        payload["success"] = False
    elif stage == "wait":
        verification.wait.side_effect = docs_deploy.DeployError("readiness timed out")
    elif stage == "audit":
        verification.audit.return_value = 1
    else:
        verification.wait.side_effect = KeyboardInterrupt
    assert docs_deploy.main([]) == (130 if stage == "interrupt" else 1)
    assert len(requests) == 1
    if stage != "audit":
        verification.audit.assert_not_called()
    log = next((tmp_path / REPORTS).glob("*/check.log")).read_text(encoding="utf-8")
    assert "verification passed" not in log
    assert "make check-site-assets" in log
    assert hook not in log


def test_check_only_needs_no_hook_and_preserves_each_report(
    tmp_path, monkeypatch, verification
):
    monkeypatch.chdir(tmp_path)
    for _ in range(2):
        assert docs_deploy.main(["--check-only", "--hardware-tag", TAG]) == 0
    verification.wait.assert_not_called()
    assert verification.audit.call_count == 2
    assert len(list((tmp_path / REPORTS).glob("*/check.log"))) == 2
    assert not list((tmp_path / REPORTS).glob("*/deployment.json"))


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        now = 0

        def sleep(self, duration):
            self.now += duration

    clock = Clock()
    monkeypatch.setattr(docs_deploy.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(docs_deploy.time, "sleep", clock.sleep)
    return clock


@pytest.fixture
def markers(monkeypatch, clock):
    """Fresh reads at the HTTP boundary; no real sleeps or external network."""
    requested = []
    values = iter([])

    def open_marker(request, timeout):
        requested.append((request.full_url, timeout))
        value = next(values)
        if isinstance(value, Exception):
            raise value
        response = io.BytesIO(value)
        response.status = 200
        return response

    def configure(sequence):
        nonlocal values
        values = iter(sequence)
        return requested

    monkeypatch.setattr(docs_deploy, "build_opener", lambda *_: Mock(open=open_marker))
    return configure


def test_same_release_redeploy_waits_for_unique_url_before_production(markers, clock):
    requested = markers(
        [
            docs_deploy.HTTPError(DEPLOYMENT, 404, "not yet", {}, None),
            b"old-release",
            TAG.encode(),
            TAG.encode(),
        ]
    )
    docs_deploy.wait_for_deployment(DEPLOYMENT, TAG, 45, 15)
    assert clock.now == 30
    assert [url.split("/.release-version")[0] for url, _ in requested] == [
        DEPLOYMENT,
        DEPLOYMENT,
        DEPLOYMENT,
        docs_deploy.PRODUCTION,
    ]
    assert all("?check=" in url for url, _ in requested)


def test_readiness_has_one_deadline_across_deployment_and_production(markers, clock):
    requested = markers([b"old", TAG.encode(), b"old"])
    with pytest.raises(docs_deploy.DeployError, match="timed out.*docs.physiclaw.ai"):
        docs_deploy.wait_for_deployment(DEPLOYMENT, TAG, 25, 15)
    assert clock.now == 25
    assert len(requested) == 3
    assert all(timeout <= 10 for _, timeout in requested)


@pytest.mark.parametrize("status", [301, 302, 401, 403])
def test_readiness_does_not_follow_redirects_or_hide_access_errors(markers, status):
    markers([docs_deploy.HTTPError(DEPLOYMENT, status, "no", {}, None)])
    with pytest.raises(docs_deploy.DeployError, match=f"HTTP {status}"):
        docs_deploy.wait_for_deployment(DEPLOYMENT, TAG, 30, 15)


def test_readiness_retries_network_errors_but_not_forever(markers, clock):
    markers([OSError("network"), b"wrong"])
    with pytest.raises(docs_deploy.DeployError, match="timed out"):
        docs_deploy.wait_for_deployment(DEPLOYMENT, TAG, 20, 15)
    assert clock.now == 20


def run_docs_command(tmp_path, argv):
    with owned_process(
        argv,
        cwd=tmp_path,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    ) as owner:
        process = owner.process
        try:
            stdout, stderr = process.communicate(timeout=60)
        except subprocess.TimeoutExpired as exc:
            owner.stop(timeout=0)
            stdout, stderr = process.communicate(timeout=5)
            pytest.fail(f"{exc}\nstdout:\n{stdout}\nstderr:\n{stderr}")
        return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


@pytest.mark.parametrize(
    "target,status,audit_result",
    [("docs-deploy", 200, 0), ("docs-deploy", 500, 0), ("check-site-assets", 200, 1)],
)
def test_make_target_propagates_result_without_extra_deployments(
    tmp_path, docs_hook, target, status, audit_result
):
    hook, requests, _ = docs_hook
    # Exercise real HTTP and Make's external driver path; replace only readiness
    # and large asset downloads in the child process.
    driver = tmp_path / "driver with spaces.py"
    driver.write_text(
        f"import sys\nsys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "from scripts import docs_deploy\n"
        "docs_deploy.wait_for_deployment = lambda *args: None\n"
        f"docs_deploy.audit_sites = lambda *args: {audit_result}\n"
        "sys.exit(docs_deploy.main())\n",
        encoding="utf-8",
    )
    result = run_docs_command(
        tmp_path,
        [
            "make",
            "--no-print-directory",
            "-f",
            str(REPO_ROOT / "Makefile"),
            target,
            f"PYTHON={Path(sys.executable).as_posix()}",
            f"DOCS_DEPLOY_SCRIPT={driver.as_posix()}",
        ],
    )
    output = result.stdout + result.stderr
    assert (result.returncode == 0) is (status == 200 and audit_result == 0), output
    assert requests == ([("POST", "/secret")] if target == "docs-deploy" else [])
    assert hook not in output
    if audit_result:
        assert "verification failed" in result.stderr


def test_docs_hook_missing_configuration_fails(tmp_path):
    result = run_docs_command(
        tmp_path, [sys.executable, str(REPO_ROOT / "scripts/docs_deploy.py")]
    )
    assert result.returncode == 1
    assert ".docs-deploy-hook missing" in result.stderr


def test_malformed_hook_does_not_disclose_secret_in_traceback(tmp_path):
    (tmp_path / ".docs-deploy-hook").write_text(
        "http://127.0.0.1/secret token", encoding="utf-8"
    )
    result = run_docs_command(
        tmp_path,
        [
            sys.executable,
            str(REPO_ROOT / "scripts/docs_deploy.py"),
            "--hardware-tag",
            TAG,
        ],
    )
    assert result.returncode == 1
    assert "Docs deploy hook request failed" in result.stderr
    assert "secret token" not in result.stdout + result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("error", [BadStatusLine, InvalidURL])
def test_http_protocol_errors_are_reported_without_secret(mocker, error):
    mocker.patch.object(
        docs_deploy, "build_opener"
    ).return_value.open.side_effect = error("secret response")
    with pytest.raises(docs_deploy.DeployError, match="hook request failed") as raised:
        docs_deploy.trigger_hook("http://127.0.0.1/secret")
    assert "secret" not in str(raised.value)


@pytest.mark.parametrize(
    "option,value",
    [
        ("--wait-timeout", "nan"),
        ("--wait-timeout", "0"),
        ("--poll-interval", "inf"),
        ("--hardware-tag", "invalid"),
    ],
)
def test_invalid_arguments_fail_before_deploying(option, value, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as error:
        docs_deploy.main([option, value])
    assert error.value.code == 2
    assert not (tmp_path / REPORTS).exists()


@pytest.mark.parametrize("body", [b"", b"secret response", b"[]", b"null"])
def test_unusable_hook_response_fails_without_exposing_body(mocker, body, capsys):
    response = mocker.patch.object(
        docs_deploy, "build_opener"
    ).return_value.open.return_value.__enter__.return_value
    response.status = 200
    response.read.return_value = body
    with pytest.raises(docs_deploy.DeployError, match="deployment response") as error:
        docs_deploy.trigger_hook("https://example.test/secret-hook")
    assert "secret" not in str(error.value)
    assert not capsys.readouterr().out


def test_cannot_trigger_deploy_without_resolving_expected_release(
    docs_hook, verification
):
    _, requests, _ = docs_hook
    verification.resolve.side_effect = ValueError("no published hardware release found")
    assert docs_deploy.main([]) == 1
    assert requests == []
    verification.wait.assert_not_called()
    verification.audit.assert_not_called()
