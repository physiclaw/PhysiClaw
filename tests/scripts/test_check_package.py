"""Release artifacts and retries must fail closed on missing or different bytes."""

import hashlib
import http.client
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import urllib.error
import urllib.parse
import zipfile
from contextlib import nullcontext
from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace

import pytest
from ruamel.yaml import YAML

from scripts import check_package
from tests.timing_helpers import PROCESS_TIMEOUT


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
    monkeypatch.setattr(
        check_package, "select_upgrade_baseline", lambda version: "1.2.2"
    )
    check_package.record(bundle)
    return bundle


def test_manifest_records_both_files_version_and_commit(bundle):
    manifest = check_package.verify_local(bundle)
    assert manifest["version"] == "1.2.3"
    assert manifest["upgrade_from"] == "1.2.2"
    assert manifest["commit"] == "a" * 40
    assert set(manifest["files"]) == check_package.filenames("1.2.3")


@pytest.mark.parametrize(
    "candidate,versions,expected",
    [
        ("1.11.0", {"1.9.0": [False], "1.11.0": [False], "1.10.0": [False]}, "1.10.0"),
        ("1.2.3rc1", {"1.2.2": [False], "1.2.3": [False], "1.2.3b1": [False]}, "1.2.2"),
        (
            "1.2.4",
            {"1.2.0": [False], "1.2.1": [True], "1.2.2": [""], "1.2.3": ["withdrawn"]},
            "1.2.0",
        ),
        ("1.2.3", {"1.2.1": [False], "1.2.2": [True, False]}, "1.2.2"),
        ("1.2.3", {"1.2.0": [False], "1.2.1.post1": [False], "1.2.2": []}, "1.2.0"),
        ("1.2.3", {"1.2.2": [True], "1.2.3": [False], "1.2.4": [False]}, None),
        ("1.2.3", {}, None),
    ],
)
def test_upgrade_baseline_selects_latest_available_older_stable_release(
    monkeypatch, candidate, versions, expected
):
    index = {
        "versions": list(versions),
        "files": [
            {"filename": name, "yanked": yanked}
            for version, statuses in versions.items()
            for name, yanked in zip(
                sorted(check_package.filenames(version)), statuses, strict=False
            )
        ],
    }

    def fetch(request, *, timeout):
        assert request.full_url == "https://pypi.org/simple/physiclaw/"
        assert request.get_header("Accept") == "application/vnd.pypi.simple.v1+json"
        assert request.get_header("Cache-control") == "no-cache"
        assert timeout == 30
        return io.BytesIO(json.dumps(index).encode())

    monkeypatch.setattr(check_package.urllib.request, "urlopen", fetch)
    if expected is None:
        with pytest.raises(ValueError, match="No published stable upgrade baseline"):
            check_package.select_upgrade_baseline(candidate)
    else:
        assert check_package.select_upgrade_baseline(candidate) == expected


def test_upgrade_baseline_does_not_hide_index_failures(mocker):
    mocker.patch.object(
        check_package.urllib.request,
        "urlopen",
        side_effect=urllib.error.URLError("offline"),
    )
    with pytest.raises(urllib.error.URLError, match="offline"):
        check_package.select_upgrade_baseline("1.2.3")


@pytest.mark.parametrize("action", ["record", "check", "smoke", "verify-pypi"])
def test_upgrade_flag_cannot_silently_skip_upgrade_checks(monkeypatch, capsys, action):
    monkeypatch.setattr(sys, "argv", ["check_package.py", action, "--upgrade"])
    with pytest.raises(SystemExit) as error:
        check_package.main()
    assert error.value.code == 2
    assert "--upgrade requires smoke-installer" in capsys.readouterr().err


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
    monkeypatch.setenv("PHYSICLAW_PYTHON", "not-the-test-interpreter")
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
        assert (
            env["PHYSICLAW_PYTHON"]
            == f"{sys.version_info.major}.{sys.version_info.minor}"
        )
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


@pytest.mark.slow
@pytest.mark.parametrize(
    "damaged,phase",
    [
        (None, "installer"),
        *((name, "installer") for name in check_package.UPGRADE_FILES),
        ("log/sessions/upgrade-canary/events.jsonl", "reload"),
    ],
)
def test_upgrade_checks_real_state_and_rejects_data_loss(
    bundle, tmp_path, monkeypatch, damaged, phase
):
    monkeypatch.setattr(
        check_package,
        "select_upgrade_baseline",
        lambda *_: pytest.fail("Install jobs must reuse the recorded baseline"),
    )
    native_run = subprocess.run
    calls = []
    scripts = []

    def run(command, **kwargs):
        calls.append((command, kwargs["env"].copy()))
        home = Path(kwargs["env"]["PHYSICLAW_HOME"])
        result = subprocess.CompletedProcess(command, 0)
        current_phase = None
        if len(command) == 4 and command[2] == "-c":
            # Execute state creation and validation against real application
            # APIs. Only the external package installation is substituted.
            scripts.append(command[3])
            result = native_run([sys.executable, *command[1:]], **kwargs)
            if (
                command[3] == check_package.UPGRADE_CHECK
                and "UV_CONSTRAINT" in kwargs["env"]
            ):
                current_phase = "reload"
        elif command[0] in {"bash", "powershell"}:
            assert all((home / name).is_file() for name in check_package.UPGRADE_FILES)
            current_phase = "installer"
        if damaged and current_phase == phase:
            path = home / damaged
            # Still parseable: detect changed bytes even if reload succeeds.
            path.write_bytes(path.read_bytes() + b"\n")
        return result

    def server(cli, root, env):
        (root / "server-1.log").write_text("server stopped cleanly\n", encoding="utf-8")

    monkeypatch.setattr(check_package.subprocess, "run", run)
    monkeypatch.setattr(check_package, "smoke_server", server)
    logs = tmp_path / "retained logs"
    with (
        pytest.raises(ValueError, match="Upgrade changed user data")
        if damaged
        else nullcontext()
    ):
        check_package.smoke_installer(bundle, upgrade=True, log_dir=logs)
    command, env = calls[0]
    assert command[:5] == ["uv", "--no-config", "tool", "install", "physiclaw==1.2.2"]
    assert "UV_CONSTRAINT" not in env
    assert calls[1][0][-1] == "1.2.2"  # Verify the recorded baseline before seeding.
    assert scripts == [
        check_package.UPGRADE_SEED,
        check_package.UPGRADE_CHECK,
        check_package.UPGRADE_CHECK,
    ]
    assert (logs / "server-1.log").read_text(
        encoding="utf-8"
    ) == "server stopped cleanly\n"


@pytest.mark.parametrize("baseline", [None, "1.2.3", "1.2.4", "1.2.3rc1"])
def test_upgrade_rejects_missing_or_ineligible_baseline(bundle, mocker, baseline):
    manifest = bundle / "manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["upgrade_from"] = baseline
    manifest.write_text(json.dumps(data), encoding="utf-8")
    run = mocker.patch.object(check_package.subprocess, "run")
    with pytest.raises(ValueError, match="upgrade baseline|baseline must be"):
        check_package.smoke_installer(bundle, upgrade=True)
    run.assert_not_called()


def test_installer_preserves_logs_when_interrupt_check_fails(bundle, tmp_path, mocker):
    mocker.patch.object(check_package.subprocess, "run")
    logs = tmp_path / "logs"

    def fail(cli, root, env):
        (root / "server-1.log").write_text("interrupt failed", encoding="utf-8")
        raise RuntimeError("interrupt failed")

    mocker.patch.object(check_package, "smoke_server", side_effect=fail)
    with pytest.raises(RuntimeError, match="interrupt failed"):
        check_package.smoke_installer(bundle, log_dir=logs)
    assert (logs / "server-1.log").read_text(encoding="utf-8") == "interrupt failed"


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
        ("shutdown-error", "failed during interrupt"),
        ("stale-state", "state survived interrupt"),
        ("control-collision", None),
        ("bridge-collision", None),
        ("control-collision-exhausted", "after 3 port collisions"),
        ("bridge-collision-exhausted", "after 3 port collisions"),
        ("permission", "exited before HTTP readiness"),
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
            import signal
            import socketserver
            import sys
            import threading
            import time
            from pathlib import Path
            from unittest.mock import patch

            mode = os.environ["SMOKE_MODE"]
            if os.name == "nt":
                signal.signal(signal.SIGBREAK, signal.default_int_handler)
            print("fixture server starting", flush=True)
            print("café 校准", flush=True)
            print("café 校准", file=sys.stderr, flush=True)
            if mode == "exit":
                sys.exit(2)
            if mode == "hang":
                threading.Event().wait()
            port = int(sys.argv[sys.argv.index("--port") + 1])
            if mode == "permission":
                print(f"cannot listen on 0.0.0.0:{port + 1}: [Errno 10013] Access denied", flush=True)
                sys.exit(1)

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

            class Server(http.server.ThreadingHTTPServer):
                allow_reuse_address = False

                def server_bind(self):
                    # Loopback fixtures must not wait for reverse DNS.
                    socketserver.TCPServer.server_bind(self)
                    self.server_name, self.server_port = self.server_address

            def listen(host, number):
                try:
                    return Server((host, number), Handler)
                except OSError as error:
                    print(f"cannot listen on {host}:{number}: {error}", flush=True)
                    sys.exit(1)

            with patch("socket.getfqdn", side_effect=AssertionError("Unexpected DNS lookup")):
                control = listen("127.0.0.1", port)
                bridge = listen("0.0.0.0", port + 1)
                state = Path(os.environ["PHYSICLAW_HOME"]) / "run" / "server.json"
                state.parent.mkdir(parents=True, exist_ok=True)
                state.write_text(json.dumps({"pid": os.getpid(), "host": "127.0.0.1", "port": port}), encoding="utf-8")
                threading.Thread(target=bridge.serve_forever, daemon=True).start()
                try:
                    control.serve_forever()
                except KeyboardInterrupt:
                    print("CONSOLE_INTERRUPT", flush=True)
                finally:
                    bridge.shutdown()
                    bridge.server_close()
                    control.server_close()
                    if mode != "stale-state":
                        state.unlink()
                if mode == "shutdown-error":
                    sys.exit(3)
            """),
        encoding="utf-8",
    )
    # Execute a real child and real HTTP requests; replace only its application.
    popen = subprocess.Popen
    children = []
    collisions = {}
    response = check_package._server_response

    def probe(opener, url, deadline):
        assert urllib.parse.urlsplit(url).port not in collisions
        return response(opener, url, deadline)

    monkeypatch.setattr(check_package, "_server_response", probe)

    def spawn(command, **kwargs):
        if command[0] == "taskkill":
            return popen(command, **kwargs)
        assert command[1:4] == ["server", "--host", "127.0.0.1"]
        assert command[-2:] == ["--no-runtime", "--no-setup-hardware"]
        if mode.endswith("collision-exhausted") or (
            mode in {"control-collision", "bridge-collision"} and not children
        ):
            control = mode.startswith("control-")
            host = "127.0.0.1" if control else "0.0.0.0"
            number = int(command[5]) + (not control)
            # Claim the port after selection, before the child binds. This
            # listener belongs to another process and must survive cleanup.
            # Match the child's address: Windows can bind loopback and wildcard
            # sockets to the same port without reporting a collision.
            collisions[number] = socket.create_server((host, number))
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
    env["PHYSICLAW_HOME"] = str(tmp_path / "home")
    # Redirected children must write UTF-8 even with an inherited legacy encoding.
    env["PYTHONIOENCODING"] = "cp1252"
    expectation = (
        pytest.raises(
            (RuntimeError, TimeoutError, ValueError, urllib.error.HTTPError),
            match=error,
        )
        if error
        else nullcontext()
    )
    try:
        with expectation:
            check_package.smoke_server(
                Path(sys.executable),
                tmp_path,
                env,
                timeout=0.2 if mode == "hang" else PROCESS_TIMEOUT,
            )
        expected_attempts = (
            3 if mode.endswith("collision-exhausted") else 2 if collisions else 1
        )
        assert len(children) == expected_attempts
        assert len({port for _, port in children}) == expected_attempts
        for attempt, (child, port) in enumerate(children, 1):
            assert child.poll() is not None
            log = (tmp_path / f"server-{attempt}.log").read_text(encoding="utf-8")
            if mode != "hang":
                assert log.count("café 校准") == 2
            for number in (port, port + 1):
                if number in collisions:
                    host = collisions[number].getsockname()[0]
                    assert child.returncode != 0
                    assert f"cannot listen on {host}:{number}:" in log
                with socket.socket() as connection:
                    connection.settimeout(0.2)
                    listening = connection.connect_ex(("127.0.0.1", number)) == 0
                    assert listening == (number in collisions)
    finally:
        for listener in collisions.values():
            listener.close()
    assert env["PYTHONIOENCODING"] == "cp1252"
    output = capsys.readouterr().out
    if mode != "hang":
        assert "fixture server starting" in output
    if mode in {"ready", "slow-page", "control-collision", "bridge-collision"}:
        assert "CONSOLE_INTERRUPT" in output
        assert "interrupt passed; state cleared and both listeners closed" in output
        assert all(
            path in output for path in ("/api/status", "/setup-hardware", "/bridge")
        )


@pytest.mark.parametrize(
    "message,expected",
    [
        ("cannot listen on 127.0.0.1:20000: [Errno 48] Address in use", 20000),
        ("cannot listen on 0.0.0.0:20001: [Errno 98] Address in use", 20001),
        ("cannot listen on 0.0.0.0:20001: [Errno 10048] Localized OS text", 20001),
        ("cannot listen on 0.0.0.0:20001: [WinError 10048] Localized OS text", 20001),
        ("port 20001 is already accepting connections on 127.0.0.1", 20001),
        ("cannot listen on 0.0.0.0:20001: [Errno 10013] Access denied", None),
        ("cannot listen on 0.0.0.0:20001: [Errno 13] Access denied", None),
        ("cannot listen on 0.0.0.0:30001: [Errno 10048] Address in use", None),
        ("unrelated failure: [Errno 10048]", None),
    ],
)
def test_bind_conflict_requires_a_known_collision_on_the_selected_pair(
    message, expected
):
    assert check_package._bind_conflict(message, 20000) == expected


def test_port_retry_keeps_the_original_startup_deadline(tmp_path, mocker):
    mocker.patch.object(check_package.time, "monotonic", side_effect=[0.0, 0.0, 1.0])
    select = mocker.patch.object(check_package, "_smoke_port", return_value=20000)
    attempt = mocker.patch.object(
        check_package, "_smoke_server_attempt", return_value=False
    )

    with pytest.raises(TimeoutError, match="did not become HTTP-ready"):
        check_package.smoke_server(Path("physiclaw"), tmp_path, {}, timeout=0.5)

    select.assert_called_once()
    attempt.assert_called_once_with(
        Path("physiclaw"), tmp_path, {}, port=20000, deadline=0.5, attempt=1
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
        check_package._stop_server(process, (1234, 1235))
    process.wait.assert_called_once_with(timeout=15)


def test_windows_shutdown_tolerates_an_already_exited_launcher(
    mocker, monkeypatch, listener_connection
):
    monkeypatch.setattr(check_package, "os", SimpleNamespace(name="nt"))
    process = mocker.Mock(pid=42, returncode=0)
    process.poll.side_effect = [None, 0]
    taskkill = mocker.patch.object(check_package.subprocess, "run")
    taskkill.return_value.returncode = 128

    check_package._stop_server(process, (1234, 1235))

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
        check_package._stop_server(process, (1234, 1235))

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
    mocker.patch.object(
        check_package,
        "pypi_index",
        return_value={
            "files": [
                {"filename": name, "hashes": item["digests"], "yanked": False}
                for name, item in entries.items()
            ]
        },
    )
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
    check_package.pypi_index.assert_not_called()
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


def test_postpublication_waits_for_index_only_after_attestation_checks(
    bundle, pypi, mocker
):
    _, _, verifier = pypi

    def wait(expected):
        assert verifier.call_count == 2
        assert expected == check_package.verify_local(bundle)["files"]
        raise TimeoutError("index unavailable")

    mocker.patch.object(check_package, "wait_for_pypi_index", side_effect=wait)
    with pytest.raises(TimeoutError, match="index unavailable"):
        check_package.verify_pypi(bundle)


@pytest.fixture
def index_state(monkeypatch, mocker):
    expected = {
        "physiclaw-1.2.3-py3-none-any.whl": "a" * 64,
        "physiclaw-1.2.3.tar.gz": "b" * 64,
    }
    state = SimpleNamespace(
        expected=expected,
        files=[
            {"filename": name, "hashes": {"sha256": checksum}, "yanked": False}
            for name, checksum in expected.items()
        ],
        now=0,
        sleeps=[],
    )

    def sleep(seconds):
        state.sleeps.append(seconds)
        state.now += seconds

    monkeypatch.setattr(
        check_package,
        "time",
        SimpleNamespace(monotonic=lambda: state.now, sleep=sleep),
    )
    state.fetch = mocker.patch.object(
        check_package, "pypi_index", return_value={"files": state.files}
    )
    return state


def test_installation_index_waits_for_both_files(index_state):
    state = index_state
    state.fetch.side_effect = [
        {"files": []},
        {"files": state.files[:1]},
        {"files": state.files},
    ]
    check_package.wait_for_pypi_index(state.expected, timeout=25)
    assert state.sleeps == [10, 10]
    assert [call.kwargs["timeout"] for call in state.fetch.call_args_list] == [
        25,
        15,
        5,
    ]


@pytest.mark.parametrize("status", [404, 408, 429, 500, 502, 503, 504])
def test_installation_index_retries_temporary_http_errors(index_state, status):
    state = index_state
    state.fetch.side_effect = [
        urllib.error.HTTPError("https://pypi.org", status, "temporary", {}, None),
        {"files": state.files},
    ]
    check_package.wait_for_pypi_index(state.expected)
    assert state.sleeps == [10]


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.URLError("offline"),
        TimeoutError("slow"),
        ConnectionResetError(),
        http.client.IncompleteRead(b"partial"),
    ],
)
def test_installation_index_retries_connection_errors(index_state, error):
    state = index_state
    state.fetch.side_effect = [error, {"files": state.files}]
    check_package.wait_for_pypi_index(state.expected)
    assert state.sleeps == [10]


@pytest.mark.parametrize("status", [400, 401, 403, 410])
def test_installation_index_rejects_permanent_http_errors(index_state, status):
    state = index_state
    state.fetch.side_effect = urllib.error.HTTPError(
        "https://pypi.org", status, "permanent", {}, None
    )
    with pytest.raises(urllib.error.HTTPError):
        check_package.wait_for_pypi_index(state.expected)
    assert state.sleeps == []


@pytest.mark.parametrize("change", ["checksum", True, "", "withdrawn"])
def test_installation_index_rejects_conflicts_even_when_partial(index_state, change):
    state = index_state
    item = state.files[0]
    if change == "checksum":
        item["hashes"]["sha256"] = "0" * 64
    else:
        item["yanked"] = change
    state.fetch.return_value = {"files": [item]}
    with pytest.raises(ValueError, match="differs or is yanked"):
        check_package.wait_for_pypi_index(state.expected)
    assert state.sleeps == []


def test_installation_index_exhausts_one_deadline(index_state):
    state = index_state
    state.fetch.return_value = {"files": []}
    with pytest.raises(TimeoutError, match="not ready after 25s.*physiclaw"):
        check_package.wait_for_pypi_index(state.expected, timeout=25)
    assert state.sleeps == [10, 10, 5]
    assert state.fetch.call_count == 3


def test_installation_index_cannot_succeed_after_deadline(index_state):
    state = index_state

    def fetch(**kwargs):
        state.now += 6
        return {"files": state.files}

    state.fetch.side_effect = fetch
    with pytest.raises(TimeoutError, match="not ready after 5s"):
        check_package.wait_for_pypi_index(state.expected, timeout=5)
    state.fetch.assert_called_once_with(timeout=5)
    assert state.sleeps == []


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
    jobs = release["jobs"]
    assert release["permissions"] == {"contents": "read"}
    assert release["concurrency"]["cancel-in-progress"] is False
    assert jobs["ci"]["needs"] == "validate"
    validation = jobs["validate"]["steps"][-1]
    assert validation["if"] == "github.ref_type == 'tag'"
    assert (
        validation["run"]
        == "uv run --no-project python scripts/release.py validate-tag"
    )
    assert jobs["publish"]["permissions"] == {"id-token": "write"}
    assert jobs["publish"]["environment"]["name"] == "pypi"
    assert set(jobs["publish"]["needs"]) == {"ci", "prepublish"}
    assert jobs["prepublish"]["needs"] == "ci"
    assert "github.event_name == 'push'" in jobs["prepublish"]["if"]
    assert "workflow_call" in ci["on"]
    assert ci["jobs"]["package"]["uses"] == "./.github/workflows/package.yml"
    # The privileged job cannot run project code or install dependencies.
    steps = jobs["publish"]["steps"]
    assert len(steps) == 2
    assert steps[0]["uses"].startswith("actions/download-artifact@")
    assert steps[0]["with"]["artifact-ids"] == "${{ needs.ci.outputs.artifact-id }}"
    assert steps[1]["uses"].startswith("pypa/gh-action-pypi-publish@")
    assert steps[1]["with"]["attestations"] is True
    assert set(jobs["verify"]["needs"]) == {"ci", "publish"}
    verification, install = jobs["verify"]["steps"][-2:]
    assert verification["run"].endswith("scripts/check_package.py verify-pypi")
    assert "--refresh-package physiclaw" in install["run"]
    assert install["run"].count("--default-index https://pypi.org/simple") == 2
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
            timeout=PROCESS_TIMEOUT,
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
