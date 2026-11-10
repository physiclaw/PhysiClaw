"""Release artifacts and retries must fail closed on missing or different bytes."""

import hashlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import urllib.error
import zipfile
from contextlib import nullcontext
from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace

import pytest
from ruamel.yaml import YAML

from scripts import check_package


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in ("GITHUB_SHA", "GITHUB_REF", "GITHUB_REF_TYPE"):
        monkeypatch.delenv(name, raising=False)
    project = b'[project]\nname = "physiclaw"\nversion = "1.2.3"\n'
    (tmp_path / "pyproject.toml").write_bytes(project)
    bundle = tmp_path / "bundle"
    dist = bundle / "dist"
    dist.mkdir(parents=True)
    with zipfile.ZipFile(dist / "physiclaw-1.2.3-py3-none-any.whl", "w") as archive:
        for resource in check_package.RESOURCES:
            archive.writestr("physiclaw/" + resource, "resource")
        archive.writestr("physiclaw-1.2.3.dist-info/licenses/LICENSE", "MIT")
        archive.writestr(
            "physiclaw-1.2.3.dist-info/METADATA", "Name: physiclaw\nVersion: 1.2.3\n"
        )
    with tarfile.open(dist / "physiclaw-1.2.3.tar.gz", "w:gz") as archive:
        member = tarfile.TarInfo("physiclaw-1.2.3/pyproject.toml")
        member.size = len(project)
        archive.addfile(member, io.BytesIO(project))
    monkeypatch.setattr(
        check_package.subprocess, "check_output", lambda *a, **kw: "a" * 40
    )
    check_package.record(bundle)
    return bundle


def test_manifest_records_both_files_version_and_commit(bundle):
    manifest = check_package.verify_local(bundle)
    assert manifest["version"] == "1.2.3"
    assert manifest["commit"] == "a" * 40
    assert set(manifest["files"]) == check_package.filenames("1.2.3")


@pytest.mark.parametrize("change", ["commit", "version"])
def test_ci_rejects_bundle_from_another_commit_or_tag(bundle, monkeypatch, change):
    monkeypatch.setenv("GITHUB_SHA", "a" * 40 if change == "version" else "b" * 40)
    monkeypatch.setenv("GITHUB_REF_TYPE", "tag")
    monkeypatch.setenv(
        "GITHUB_REF", "refs/tags/v9.0.0" if change == "version" else "refs/tags/v1.2.3"
    )
    with pytest.raises(ValueError, match="does not match"):
        check_package.verify_local(bundle)


def test_ci_accepts_bundle_for_its_commit_and_tag(bundle, monkeypatch):
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setenv("GITHUB_REF_TYPE", "tag")
    monkeypatch.setenv("GITHUB_REF", "refs/tags/v1.2.3")
    assert check_package.verify_local(bundle)["version"] == "1.2.3"


@pytest.mark.parametrize("change", ["corrupt", "missing", "extra", "manifest"])
def test_changed_bundle_is_rejected(bundle, change):
    path = bundle / "dist" / "physiclaw-1.2.3.tar.gz"
    if change == "corrupt":
        path.write_bytes(b"different")
    elif change == "missing":
        path.unlink()
    elif change == "extra":
        (path.parent / "old.whl").write_bytes(b"stale")
    else:
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        manifest["files"]["../../unexpected"] = "0" * 64
        (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(
        ValueError, match="checksum mismatch|do not match|must contain exactly"
    ):
        check_package.verify_local(bundle)


def test_record_rejects_missing_runtime_resources(bundle):
    wheel = bundle / "dist" / "physiclaw-1.2.3-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("physiclaw/__init__.py", "")
    with pytest.raises(ValueError, match="resources or license"):
        check_package.record(bundle)


@pytest.mark.parametrize("invalid_output", [False, True])
def test_package_smoke_uses_utf8_and_rejects_invalid_cli_output(
    bundle, monkeypatch, capsys, invalid_output
):
    monkeypatch.setenv("PYTHONUTF8", "0")
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")
    native_run = subprocess.run
    help_calls = []

    def run(command, **kwargs):
        if command[-1] == "--help":
            help_calls.append(command)
            script = (
                "import sys; sys.stdout.buffer.write(bytes([0x97]))"
                if invalid_output
                else "print('PhysiClaw \\u2014 caf\\u00e9')"
            )
            return native_run([sys.executable, "-c", script], **kwargs)
        output = "1.2.3\n" if command[-1] == "--version" else ""
        return subprocess.CompletedProcess(
            command, 0, stdout=output if kwargs.get("text") else output.encode()
        )

    monkeypatch.setattr(check_package.subprocess, "run", run)
    with pytest.raises(UnicodeDecodeError) if invalid_output else nullcontext():
        check_package.smoke(bundle)
    assert len(help_calls) == (1 if invalid_output else 2)
    if not invalid_output:
        assert capsys.readouterr().out.count("PhysiClaw — café") == 2


@pytest.mark.parametrize("output", ["install failed\nretry", b"install failed\nretry"])
def test_failed_package_command_keeps_readable_diagnostics(
    monkeypatch, mocker, capsys, output
):
    monkeypatch.setattr(sys, "argv", ["check_package.py", "smoke"])
    mocker.patch.object(
        check_package,
        "smoke",
        side_effect=subprocess.CalledProcessError(7, ["uv", "pip", "install"], output),
    )

    with pytest.raises(SystemExit, match="Package check failed"):
        check_package.main()
    assert capsys.readouterr().err == "install failed\nretry\n"


def test_installer_uses_built_wheel_and_runs_doctor_in_isolation(
    bundle, tmp_path, monkeypatch, mocker
):
    temporary = tmp_path / "installer with spaces"
    temporary.mkdir()
    monkeypatch.setattr(check_package.tempfile, "tempdir", str(temporary))
    for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        monkeypatch.setenv(key, "must-not-leak")
    monkeypatch.setenv("PHYSICLAW_DRY_RUN", "1")
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")
    server = mocker.patch.object(check_package, "smoke_server")
    calls = []

    def run(command, *, cwd, env, check, timeout):
        assert check
        assert cwd.parent == temporary
        assert not {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"} & env.keys()
        assert env["PHYSICLAW_HOME"] == str(cwd / "home")
        assert env["PHYSICLAW_VERSION"] == "1.2.3"
        assert env["PHYSICLAW_DRY_RUN"] == "0"
        assert env["PHYSICLAW_DISABLE_UPDATE_CHECK"] == "1"
        assert env["PYTHONIOENCODING"] == "utf-8"
        assert env["UV_TOOL_DIR"] == str(cwd / "tools")
        assert env["UV_TOOL_BIN_DIR"] == str(cwd / "bin")
        assert env["PATH"].split(os.pathsep)[0] == str(cwd / "bin")
        constraints = cwd / "constraints.txt"
        assert env["UV_CONSTRAINT"] == constraints.as_uri()
        wheel = bundle / "dist" / "physiclaw-1.2.3-py3-none-any.whl"
        assert constraints.read_text(encoding="utf-8") == (
            f"physiclaw @ {wheel.as_uri()}\n"
        )
        calls.append(command)

    monkeypatch.setattr(check_package.subprocess, "run", run)
    check_package.smoke_installer(bundle)
    repository = Path(__file__).resolve().parents[2]
    script = repository / ("install.ps1" if os.name == "nt" else "install.sh")
    assert calls[0][-1] == str(script)
    assert calls[-1][1:] == ["doctor"]
    assert Path(calls[-1][0]).is_relative_to(temporary)
    assert Path(calls[-1][0]).parent.name == "bin"
    server.assert_called_once()
    cli, root, env = server.call_args.args
    assert str(cli) == calls[-1][0]
    assert root.parent == temporary
    assert env["PHYSICLAW_HOME"] == str(root / "home")


@pytest.mark.parametrize("failure", ["installer", "doctor"])
def test_installer_smoke_propagates_failures(bundle, mocker, failure):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if failure == "installer" or command[-1] == "doctor":
            raise subprocess.CalledProcessError(1, command)

    mocker.patch.object(check_package.subprocess, "run", side_effect=run)
    with pytest.raises(subprocess.CalledProcessError):
        check_package.smoke_installer(bundle)
    if failure == "installer":
        assert len(commands) == 1
    else:
        assert commands[-1][-1] == "doctor"


def test_installer_rejects_corrupt_wheel_before_running(bundle, mocker):
    (bundle / "dist" / "physiclaw-1.2.3-py3-none-any.whl").write_bytes(b"corrupt")
    run = mocker.patch.object(check_package.subprocess, "run")
    with pytest.raises(ValueError, match="checksum mismatch"):
        check_package.smoke_installer(bundle)
    run.assert_not_called()


@pytest.mark.parametrize(
    "mode,error",
    [
        ("ready", None),
        ("slow-page", None),
        ("exit", "exited before HTTP readiness"),
        ("hang", "did not become HTTP-ready"),
        ("bad-status", "Unexpected fresh-server status"),
        ("bad-json", "Expecting value"),
        ("http-error", "HTTP Error 503"),
        ("redirect", "Unexpected HTTP response"),
        ("bad-page", "did not serve the packaged /setup-hardware page"),
        ("bad-bridge", "did not serve the packaged /bridge page"),
    ],
)
def test_server_smoke_checks_http_and_always_stops(
    tmp_path, monkeypatch, capsys, mode, error
):
    script = tmp_path / "server-fixture.py"
    script.write_text(
        dedent("""\
            import http.server
            import json
            import os
            import sys
            import threading
            import time

            mode = os.environ["SMOKE_MODE"]
            print("fixture server starting", flush=True)
            if mode == "exit":
                sys.exit(2)
            if mode == "hang":
                time.sleep(120)
            port = int(sys.argv[sys.argv.index("--port") + 1])

            class Handler(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    print(self.path, flush=True)
                    if mode == "slow-page" and self.path == "/setup-hardware":
                        time.sleep(1.2)  # mDNS fallback plus page rendering.
                    if mode == "redirect" and self.path == "/api/status":
                        self.send_response(302)
                        self.send_header("Location", "/redirect-status")
                        self.end_headers()
                        return
                    self.send_response(503 if mode == "http-error" else 200)
                    if self.path in ("/api/status", "/redirect-status"):
                        self.send_header("Content-Type", "application/json")
                        body = json.dumps({"ready": False, "arm": False, "camera": False})
                        if mode == "bad-status":
                            body = "{}"
                        if mode == "bad-json":
                            body = "not JSON"
                    else:
                        self.send_header("Content-Type", "text/html")
                        body = "<html>packaged page</html>"
                        if (mode == "bad-page" and self.path == "/setup-hardware") or (
                            mode == "bad-bridge" and self.path == "/bridge"
                        ):
                            body = "missing page"
                    self.end_headers()
                    self.wfile.write(body.encode())

            bridge = http.server.ThreadingHTTPServer(("127.0.0.1", port + 1), Handler)
            threading.Thread(target=bridge.serve_forever, daemon=True).start()
            http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
            """),
        encoding="utf-8",
    )
    # Execute a real child and real HTTP requests; replace only its application.
    popen = subprocess.Popen
    children = []

    def spawn(command, **kwargs):
        if command[0] == "taskkill":
            return popen(command, **kwargs)
        assert command[1:4] == ["server", "--host", "127.0.0.1"]
        assert command[-2:] == ["--no-runtime", "--no-setup-hardware"]
        child = popen([sys.executable, str(script), *command[1:]], **kwargs)
        children.append((child, int(command[5])))
        return child

    monkeypatch.setattr(check_package.subprocess, "Popen", spawn)
    # A runner's proxy settings must not intercept local readiness probes.
    for key in ("HTTP_PROXY", "http_proxy"):
        monkeypatch.setenv(key, "http://127.0.0.1:1")
    for key in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(key, "")
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"PATH", "SYSTEMROOT", "TEMP", "TMP"}
    }
    env["SMOKE_MODE"] = mode
    expectation = (
        pytest.raises(
            (RuntimeError, TimeoutError, ValueError, urllib.error.HTTPError),
            match=error,
        )
        if error
        else nullcontext()
    )
    with expectation:
        check_package.smoke_server(
            Path(sys.executable), tmp_path, env, timeout=0.2 if mode == "hang" else 10
        )
    assert len(children) == 1
    child, port = children[0]
    assert child.poll() is not None
    for number in (port, port + 1):
        with socket.socket() as connection:
            connection.settimeout(0.2)
            assert connection.connect_ex(("127.0.0.1", number)) != 0
    output = capsys.readouterr().out
    if mode != "hang":
        assert "fixture server starting" in output
    if mode in {"ready", "slow-page"}:
        assert all(
            path in output for path in ("/api/status", "/setup-hardware", "/bridge")
        )


def test_http_response_cannot_pass_after_the_deadline(mocker):
    opener = mocker.MagicMock()
    response = opener.open.return_value.__enter__.return_value
    response.status = 200
    response.geturl.return_value = "http://127.0.0.1:1234/api/status"
    mocker.patch.object(check_package.time, "monotonic", side_effect=[0.0, 2.0])

    with pytest.raises(TimeoutError, match="did not become HTTP-ready"):
        check_package._server_response(opener, response.geturl(), deadline=1.0)
    assert opener.open.call_args.kwargs["timeout"] == 1.0


@pytest.fixture
def listener_connection(mocker):
    sockets = mocker.patch.object(check_package.socket, "socket")
    connection = sockets.return_value.__enter__.return_value
    connection.connect_ex.return_value = 1
    return connection


def test_shutdown_checks_for_surviving_listeners_after_parent_exit(
    mocker, listener_connection
):
    process = mocker.Mock(returncode=0)
    process.poll.return_value = 0
    listener_connection.connect_ex.return_value = 0
    mocker.patch.object(check_package.time, "monotonic", side_effect=[0.0, 6.0])

    with pytest.raises(RuntimeError, match="listener 1234 survived shutdown"):
        check_package._stop_server(process, 1234)
    process.wait.assert_called_once_with(timeout=15)


def test_windows_shutdown_tolerates_an_already_exited_launcher(
    mocker, monkeypatch, listener_connection
):
    monkeypatch.setattr(check_package, "os", SimpleNamespace(name="nt"))
    process = mocker.Mock(pid=42, returncode=0)
    process.poll.side_effect = [None, 0]
    taskkill = mocker.patch.object(check_package.subprocess, "run")
    taskkill.return_value.returncode = 128

    check_package._stop_server(process, 1234)

    taskkill.return_value.check_returncode.assert_not_called()
    process.wait.assert_called_once_with(timeout=15)


@pytest.mark.parametrize("platform", ["posix", "nt"])
def test_shutdown_timeout_forces_cleanup_and_still_fails(
    mocker, monkeypatch, listener_connection, platform
):
    monkeypatch.setattr(check_package, "os", SimpleNamespace(name=platform))
    process = mocker.Mock(pid=42, returncode=0)
    process.poll.return_value = None
    process.wait.side_effect = [subprocess.TimeoutExpired("server", 15), 0]
    taskkill = mocker.patch.object(check_package.subprocess, "run")
    taskkill.return_value.returncode = 0

    with pytest.raises(subprocess.TimeoutExpired):
        check_package._stop_server(process, 1234)

    process.kill.assert_called_once_with()
    assert process.wait.call_count == 2
    if platform == "nt":
        assert taskkill.call_count == 2
        assert all(
            call.args[0][-2:] == ["/T", "/F"] for call in taskkill.call_args_list
        )


@pytest.fixture
def pypi(bundle, monkeypatch, mocker):
    data = {p.name: p.read_bytes() for p in (bundle / "dist").iterdir()}
    entries = {
        name: {
            "url": f"https://files.pythonhosted.org/{name}",
            "digests": {"sha256": hashlib.sha256(content).hexdigest()},
            "yanked": False,
        }
        for name, content in data.items()
    }
    monkeypatch.setattr(check_package, "published_files", lambda version: entries)
    monkeypatch.setattr(
        check_package.urllib.request,
        "urlopen",
        lambda url, **kw: io.BytesIO(data[url.rsplit("/", 1)[1]]),
    )
    monkeypatch.setattr(check_package.time, "sleep", lambda seconds: None)
    verifier = mocker.patch.object(check_package.subprocess, "run")
    return entries, data, verifier


def test_postpublication_checks_both_downloads_and_attestations(bundle, pypi):
    entries, _, verifier = pypi
    check_package.verify_pypi(bundle)
    assert verifier.call_count == 2
    assert {call.args[0][-1] for call in verifier.call_args_list} == {
        item["url"] for item in entries.values()
    }
    assert all(
        check_package.REPOSITORY in call.args[0] for call in verifier.call_args_list
    )


def test_partial_retry_verifies_existing_file_but_requires_complete_postcheck(
    bundle, pypi
):
    entries, _, verifier = pypi
    entries.pop("physiclaw-1.2.3.tar.gz")
    check_package.verify_pypi(bundle, allow_missing=True)
    assert verifier.call_count == 1
    with pytest.raises(ValueError, match="both release files"):
        check_package.verify_pypi(bundle)


def test_new_release_has_no_existing_files_to_skip(bundle, pypi):
    entries, _, verifier = pypi
    entries.clear()
    check_package.verify_pypi(bundle, allow_missing=True)
    verifier.assert_not_called()


@pytest.mark.parametrize(
    "change", ["checksum", "bytes", "yanked", "host", "unexpected"]
)
def test_retry_rejects_conflicting_remote_state(bundle, pypi, change):
    entries, data, _ = pypi
    name = next(iter(entries))
    if change == "checksum":
        entries[name]["digests"]["sha256"] = "0" * 64
    elif change == "bytes":
        data[name] = b"different download"
    elif change == "yanked":
        entries[name]["yanked"] = True
    elif change == "host":
        entries[name]["url"] = "https://example.invalid/file.whl"
    else:
        entries["unexpected.whl"] = entries[name]
    with pytest.raises(
        ValueError, match="checksum mismatch|differs or is yanked|Unexpected|unexpected"
    ):
        check_package.verify_pypi(bundle, allow_missing=True)


def test_retry_rejects_unverifiable_provenance(bundle, pypi):
    _, _, verifier = pypi
    verifier.side_effect = subprocess.CalledProcessError(1, ["pypi-attestations"])
    with pytest.raises(subprocess.CalledProcessError):
        check_package.verify_pypi(bundle, allow_missing=True)


@pytest.mark.parametrize("status", [404, 403, 500])
def test_only_not_found_is_treated_as_an_unpublished_version(monkeypatch, status):
    def fail(*args, **kwargs):
        raise urllib.error.HTTPError("https://pypi.org", status, "error", {}, None)

    monkeypatch.setattr(check_package.urllib.request, "urlopen", fail)
    if status == 404:
        assert check_package.published_files("1.2.3") == {}
    else:
        with pytest.raises(urllib.error.HTTPError):
            check_package.published_files("1.2.3")


def test_release_permissions_and_artifact_promotion_are_isolated():
    workflows = Path(__file__).resolve().parents[2] / ".github" / "workflows"
    yaml = YAML(typ="safe")
    release = yaml.load(workflows / "release.yml")
    ci = yaml.load(workflows / "ci.yml")
    package = yaml.load(workflows / "package.yml")
    jobs = release["jobs"]
    assert release["permissions"] == {"contents": "read"}
    assert release["concurrency"]["cancel-in-progress"] is False
    assert jobs["publish"]["permissions"] == {"id-token": "write"}
    assert jobs["publish"]["environment"]["name"] == "pypi"
    assert set(jobs["publish"]["needs"]) == {"ci", "prepublish"}
    assert "github.event_name == 'push'" in jobs["prepublish"]["if"]
    assert "workflow_call" in ci["on"]
    assert ci["jobs"]["package"]["uses"] == "./.github/workflows/package.yml"
    assert set(package["jobs"]["install"]["strategy"]["matrix"]["os"]) == {
        "ubuntu-latest",
        "macos-latest",
        "windows-latest",
    }
    # The privileged job cannot run project code or install dependencies.
    steps = jobs["publish"]["steps"]
    assert len(steps) == 2
    assert steps[0]["uses"].startswith("actions/download-artifact@")
    assert steps[0]["with"]["artifact-ids"] == "${{ needs.ci.outputs.artifact-id }}"
    assert steps[1]["uses"].startswith("pypa/gh-action-pypi-publish@")
    assert steps[1]["with"]["attestations"] is True
    assert jobs["github-release"]["needs"] == ["ci", "verify"]


@pytest.fixture
def github_release_job(tmp_path):
    # This step runs on Ubuntu. Execute its real Bash with gh replaced by a local
    # fake; no GitHub requests, approvals, or publication can occur in these tests.
    if os.name == "nt" or not shutil.which("bash") or not shutil.which("jq"):
        pytest.skip("GitHub release job regression needs POSIX bash and jq")
    workflows = Path(__file__).resolve().parents[2] / ".github" / "workflows"
    workflow = YAML(typ="safe").load(workflows / "release.yml")
    script = workflow["jobs"]["github-release"]["steps"][-1]["run"]
    script = 'gh() { "$TEST_PYTHON" "$TEST_GH_SCRIPT" "$@"; }\n' + script
    assets = tmp_path / "remote-assets"
    assets.mkdir()
    state = tmp_path / "remote-release.json"
    local = tmp_path / "release-bundle"
    (local / "dist").mkdir(parents=True)
    for name in check_package.filenames("1.2.3"):
        (local / "dist" / name).write_bytes(name.encode())
    (local / "manifest.json").write_bytes(b"manifest")
    fake = tmp_path / "fake-gh.py"
    fake.write_text(
        dedent("""\
        import json, os, shutil, sys
        from pathlib import Path
        args = sys.argv[1:]
        assert args[:1] == ["release"] and args[2] == "v1.2.3", args
        assert "--clobber" not in args
        state = Path("remote-release.json")
        assets = Path("remote-assets")
        action = args[1]
        if action == "view":
            if not state.exists():
                sys.exit(1)
            data = json.loads(state.read_text(encoding="utf-8"))
            data["assets"] = [{"name": p.name} for p in assets.iterdir()]
            print(json.dumps(data))
        elif action == "create":
            assert not state.exists()
            assert {"--draft", "--verify-tag", "--latest=false"} <= set(args)
            state.write_text(json.dumps({"isDraft": True}), encoding="utf-8")
        elif action == "upload":
            assert json.loads(state.read_text(encoding="utf-8"))["isDraft"]
            source = Path(args[3])
            if source.name == os.environ.get("FAIL_ASSET"):
                sys.exit(77)
            assert not (assets / source.name).exists()
            shutil.copyfile(source, assets / source.name)
        elif action == "download":
            name = args[args.index("--pattern") + 1]
            destination = Path(args[args.index("--dir") + 1]) / name
            shutil.copyfile(assets / name, destination)
        elif action == "edit":
            assert {"--draft=false", "--latest=false"} <= set(args)
            assert len(list(assets.iterdir())) == 3
            state.write_text(json.dumps({"isDraft": False}), encoding="utf-8")
        else:
            raise AssertionError(args)
        """),
        encoding="utf-8",
    )

    def run(fail_asset=""):
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script],
            cwd=tmp_path,
            env={
                "PATH": os.environ["PATH"],
                "RUNNER_TEMP": str(tmp_path),
                "RELEASE_TAG": "v1.2.3",
                "RELEASE_VERSION": "1.2.3",
                "TEST_PYTHON": sys.executable,
                "TEST_GH_SCRIPT": str(fake),
                "FAIL_ASSET": fail_asset,
            },
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=30,
        )

    return run, state, assets


def test_github_release_retry_resumes_partial_draft_upload(github_release_job):
    run, state, assets = github_release_job
    failed = run("physiclaw-1.2.3.tar.gz")
    assert failed.returncode == 77
    assert json.loads(state.read_text(encoding="utf-8"))["isDraft"]
    assert len(list(assets.iterdir())) == 1
    retried = run()
    assert retried.returncode == 0, retried.stdout + retried.stderr
    assert not json.loads(state.read_text(encoding="utf-8"))["isDraft"]
    assert {p.name for p in assets.iterdir()} == check_package.filenames("1.2.3") | {
        "manifest.json"
    }
    # Re-running a completed job verifies the same bytes without overwriting.
    assert run().returncode == 0


@pytest.mark.parametrize("change", ["corrupt", "missing"])
def test_github_release_retry_rejects_conflicting_published_assets(
    github_release_job, change
):
    run, _, assets = github_release_job
    assert run().returncode == 0
    target = assets / "manifest.json"
    if change == "corrupt":
        target.write_bytes(b"different")
    else:
        target.unlink()
    assert run().returncode != 0
    assert (
        not target.exists()
        if change == "missing"
        else target.read_bytes() == b"different"
    )
