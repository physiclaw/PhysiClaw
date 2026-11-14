"""Check the exact distributions promoted by CI, without importing the checkout."""

import argparse
import hashlib
import http.client
import json
import os
import random
import re
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
import zipfile
from email.parser import BytesParser
from pathlib import Path

# Support both direct script execution and imports by tests/tools.
if __package__:
    from scripts import release
else:
    import release

REPOSITORY = "https://github.com/physiclaw/PhysiClaw"
UPGRADE_FILES = (
    "config.toml",
    "calibration/bundle.json",
    "macros/upgrade-canary.yml",
    "log/sessions/upgrade-canary/events.jsonl",
)
UPGRADE_SEED = """\
from physiclaw.common import config, paths
from physiclaw.core.calibration.state import Calibration
from physiclaw.macros.store import init_macro

config.set_dotted('server.save_tool_calls', 'true')
Calibration(cam_size=(640, 480), cam_rotation=-1).save()
init_macro('upgrade-canary')
session = paths.sessions_dir() / 'upgrade-canary' / 'events.jsonl'
session.parent.mkdir(parents=True, exist_ok=True)
session.write_text('{"type":"upgrade-canary","text":"café 校准"}\\n', encoding='utf-8')
"""
UPGRADE_CHECK = """\
from physiclaw.common import config
from physiclaw.core.calibration.state import Calibration
from physiclaw.macros.store import scan

assert config.load().server.save_tool_calls is True
calibration = Calibration.load()
assert calibration is not None and calibration.cam_size == (640, 480)
assert calibration.cam_rotation == -1
macro = next(entry for entry in scan() if entry.name == 'upgrade-canary')
assert macro.spec is not None and not macro.error, macro.error
"""
RESOURCES = (
    "agent/context/AGENT.md",
    "agent/context/PHYSICLAW.md",
    "static/trace/viewer.html",
    "static/studio/studio.html",
    "static/setup/setup-hardware.html",
    "static/setup/bridge.html",
    "static/setup/qr.html",
    "static/assistive-touch/test-assistive-touch.html",
    "static/vendor/qrcode-1.4.4.min.js",
    "static/vendor/qrcode-LICENSE.txt",
)


def digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def filenames(version: str) -> set[str]:
    return {f"physiclaw-{version}-py3-none-any.whl", f"physiclaw-{version}.tar.gz"}


def pypi_index(*, timeout: float = 30) -> dict:
    request = urllib.request.Request(
        "https://pypi.org/simple/physiclaw/",
        headers={
            "Accept": "application/vnd.pypi.simple.v1+json",
            "Cache-Control": "no-cache",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def select_upgrade_baseline(candidate: str) -> str:
    """Newest available stable release strictly older than this candidate."""
    candidate_key = release.version_key(candidate)
    index = pypi_index()
    available = {
        item["filename"]
        for item in index["files"]
        if item.get("yanked", False) is False
    }
    candidates = []
    for version in index["versions"]:
        try:
            key = release.version_key(version)
        except release.ReleaseError:
            continue
        if key[3] == 3 and key < candidate_key and filenames(version) & available:
            candidates.append(version)
    if not candidates:
        raise ValueError(f"No published stable upgrade baseline older than {candidate}")
    return max(candidates, key=release.version_key)


def distributions(bundle: Path) -> list[Path]:
    # uv creates a .gitignore in the output directory. It is not a distribution
    # and upload-artifact omits hidden files. Reject any other unexpected file.
    return [path for path in (bundle / "dist").iterdir() if path.name != ".gitignore"]


def record(bundle: Path) -> None:
    with Path("pyproject.toml").open("rb") as source:
        version = tomllib.load(source)["project"]["version"]
    paths = distributions(bundle)
    if {p.name for p in paths} != filenames(version):
        raise ValueError("Expected only the current wheel and source archive")
    wheel = next(p for p in paths if p.suffix == ".whl")
    with zipfile.ZipFile(wheel) as archive:
        metadata_root = f"physiclaw-{version}.dist-info"
        required = {f"physiclaw/{name}" for name in RESOURCES}
        required.add(f"{metadata_root}/licenses/LICENSE")
        if not required <= set(archive.namelist()):
            raise ValueError("Wheel is missing package resources or license")
        metadata = BytesParser().parsebytes(archive.read(f"{metadata_root}/METADATA"))
        if (metadata["Name"], metadata["Version"]) != ("physiclaw", version):
            raise ValueError("Wheel metadata does not match the release")
    sdist = next(p for p in paths if p.name.endswith(".tar.gz"))
    with tarfile.open(sdist) as archive:
        member = archive.extractfile(f"physiclaw-{version}/pyproject.toml")
        if (
            member is None
            or tomllib.loads(member.read().decode("utf-8"))["project"]["version"]
            != version
        ):
            raise ValueError("Source archive version does not match the release")
    manifest = {
        "version": version,
        # Resolve once so every OS/Python cell uses the same published baseline.
        "upgrade_from": select_upgrade_baseline(version),
        "commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "files": {p.name: digest(p) for p in sorted(paths)},
    }
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"Recorded upgrade baseline: {manifest['upgrade_from']} -> {version}",
        flush=True,
    )


def verify_local(bundle: Path) -> dict:
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    if os.environ.get("GITHUB_SHA") and manifest["commit"] != os.environ["GITHUB_SHA"]:
        raise ValueError("Artifact commit does not match this workflow run")
    if os.environ.get("GITHUB_REF_TYPE") == "tag" and os.environ.get("GITHUB_REF") != (
        f"refs/tags/v{manifest['version']}"
    ):
        raise ValueError("Artifact version does not match the release tag")
    if set(manifest["files"]) != filenames(manifest["version"]):
        raise ValueError(
            "Manifest must contain exactly the release wheel and source archive"
        )
    paths = distributions(bundle)
    if {p.name for p in paths} != set(manifest["files"]):
        raise ValueError("Distribution files do not match the manifest")
    for path in paths:
        if digest(path) != manifest["files"][path.name]:
            raise ValueError(f"Artifact checksum mismatch: {path.name}")
    return manifest


def smoke(bundle: Path) -> None:
    manifest = verify_local(bundle)
    # No editable project, dev dependencies, checkout imports, or developer config.
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
    }
    env["PYTHONIOENCODING"] = "utf-8"
    for name in sorted(manifest["files"]):
        with tempfile.TemporaryDirectory(prefix="physiclaw-package-") as temporary:
            root = Path(temporary)
            env["PHYSICLAW_HOME"] = str(root / "home")
            venv = root / "venv"
            python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            cli = python.parent / ("physiclaw.exe" if os.name == "nt" else "physiclaw")

            def run(*args: str, cwd: Path = root) -> str:
                result = subprocess.run(
                    args,
                    cwd=cwd,
                    env=env,
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=300,
                )
                # Decode on the calling thread: Windows pipe-reader exceptions
                # in text mode can otherwise leave a successful return code.
                output = result.stdout.decode("utf-8")
                print(output, end="", flush=True)
                return output

            run("uv", "--no-config", "venv", "--python", sys.executable, str(venv))
            run(
                "uv",
                "--no-config",
                "pip",
                "install",
                "--python",
                str(python),
                "--default-index",
                "https://pypi.org/simple",
                str((bundle / "dist" / name).resolve()),
            )
            run("uv", "--no-config", "pip", "check", "--python", str(python))
            probe = (
                "import importlib.metadata as m, importlib.resources as r, sys; "
                "import physiclaw, cv2, numpy, PIL, mcp; "
                "assert m.version('physiclaw') == sys.argv[1]; "
                "assert physiclaw.__version__ == sys.argv[1]; "
                "assert all(r.files('physiclaw').joinpath(p).is_file() for p in sys.argv[2:]); "
                "assert not {'build123d', 'OCP'} & sys.modules.keys()"
            )
            run(str(python), "-I", "-c", probe, manifest["version"], *RESOURCES)
            if manifest["version"] not in run(str(cli), "--version"):
                raise ValueError("Installed CLI reports the wrong version")
            run(str(cli), "--help")


def _server_response(opener, url: str, deadline: float) -> tuple[str, bytes]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Installed server did not become HTTP-ready")
    # Setup HTML can spend a full second waiting for mDNS before responding.
    with opener.open(url, timeout=min(5, remaining)) as response:
        if response.status != 200 or response.geturl() != url:
            raise ValueError(f"Unexpected HTTP response for {url}: {response.status}")
        body = response.read(1024 * 1024)
        if time.monotonic() >= deadline:
            raise TimeoutError("Installed server did not become HTTP-ready")
        return response.headers.get_content_type(), body


def _stop_server(process, ports: tuple[int, ...], *, interrupt: bool = False) -> None:
    windows = os.name == "nt"
    kill_tree = ["taskkill", "/PID", str(process.pid), "/T", "/F"]
    was_running = process.poll() is None
    try:
        if was_running:
            if interrupt:
                process.send_signal(
                    signal.CTRL_BREAK_EVENT if windows else signal.SIGINT
                )
            elif windows:
                # The Windows launcher owns a Python child. Stop the whole tree.
                result = subprocess.run(kill_tree, check=False, timeout=15)
                # taskkill can race with a process that has already exited.
                if result.returncode and process.poll() is None:
                    result.check_returncode()
            else:
                process.terminate()
    finally:
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            if windows:
                subprocess.run(kill_tree, check=False, timeout=5)
            process.kill()
            process.wait(timeout=5)
            raise
        finally:
            deadline = time.monotonic() + 5
            for number in ports:
                while True:
                    with socket.socket() as connection:
                        connection.settimeout(0.2)
                        if connection.connect_ex(("127.0.0.1", number)) != 0:
                            break
                    if time.monotonic() >= deadline:
                        raise RuntimeError(
                            f"Server listener {number} survived shutdown"
                        )
                    time.sleep(0.1)
    if interrupt and (not was_running or process.returncode != 0):
        raise RuntimeError(f"Server failed during interrupt: {process.returncode}")
    if was_running and not windows and process.returncode not in (0, -signal.SIGTERM):
        raise RuntimeError(f"Server failed during shutdown: {process.returncode}")


def _smoke_port(used: set[int]) -> int:
    # Stay below the default client ephemeral ranges, so readiness connections
    # cannot take the bridge port while the installed CLI is still starting.
    # Disjoint pairs avoid reusing either port from an earlier attempt.
    for port in random.sample(range(20000, 30000, 2), 100):
        if port in used:
            continue
        with socket.socket() as control, socket.socket() as bridge:
            try:
                control.bind(("127.0.0.1", port))
                bridge.bind(("0.0.0.0", port + 1))
            except OSError:
                continue
            return port
    raise RuntimeError("No free adjacent ports for the server smoke test")


def _bind_conflict(log: str, port: int) -> int | None:
    """Recognize only the CLI's address-in-use diagnostics for this port pair."""
    for host, number in (("127.0.0.1", port), ("0.0.0.0", port + 1)):
        # macOS, Linux and Windows use different EADDRINUSE numbers. Match the
        # number rather than localized OS text; permission errors must fail.
        if re.search(
            rf"cannot listen on {re.escape(host)}:{number}: "
            r"\[(?:Errno|WinError) (?:48|98|10048)\]",
            log,
        ) or (f"port {number} is already accepting connections on 127.0.0.1" in log):
            return number
    return None


def _smoke_server_attempt(
    cli: Path,
    root: Path,
    env: dict[str, str],
    *,
    port: int,
    deadline: float,
    attempt: int,
) -> bool:
    """Check one child; return False only when startup lost its port reservation."""

    command = [
        str(cli),
        "server",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--no-runtime",
        "--no-setup-hardware",
    ]
    log = root / f"server-{attempt}.log"
    state_file = Path(env["PHYSICLAW_HOME"]) / "run" / "server.json"
    # Local probes must bypass any HTTP proxy inherited from the runner.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with log.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(
            command,
            cwd=root,
            # The child writes directly to the file descriptor, bypassing the
            # text wrapper above. Set its encoding to match the UTF-8 log readers.
            env={**env, "PYTHONIOENCODING": "utf-8"},
            stdout=output,
            stderr=subprocess.STDOUT,
            # Target the installed Windows launcher and its Python child together.
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        conflict = None
        ready = False
        try:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    if process.returncode:
                        conflict = _bind_conflict(
                            log.read_text(encoding="utf-8", errors="replace"), port
                        )
                        if conflict is not None:
                            return False
                    raise RuntimeError(
                        f"Server exited before HTTP readiness: {process.returncode}"
                    )
                try:
                    state = json.loads(state_file.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    state = None
                # The CLI writes this private state only after binding both
                # listeners. Until then, an HTTP reply could be another server's.
                if (
                    not isinstance(state, dict)
                    or state.get("host") != "127.0.0.1"
                    or state.get("port") != port
                ):
                    time.sleep(0.1)
                    continue
                try:
                    content_type, body = _server_response(
                        opener, f"http://127.0.0.1:{port}/api/status", deadline
                    )
                    status = json.loads(body)
                    if (
                        content_type != "application/json"
                        or not isinstance(status, dict)
                        or any(
                            status.get(key) is not False
                            for key in ("ready", "arm", "camera")
                        )
                    ):
                        raise ValueError(f"Unexpected fresh-server status: {status!r}")
                    for number, path in (
                        (port, "/setup-hardware"),
                        (port + 1, "/bridge"),
                    ):
                        content_type, body = _server_response(
                            opener, f"http://127.0.0.1:{number}{path}", deadline
                        )
                        if content_type != "text/html" or b"<html" not in body.lower():
                            raise ValueError(
                                f"Server did not serve the packaged {path} page"
                            )
                    if process.poll() is not None:
                        raise RuntimeError("Server exited during HTTP readiness checks")
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Installed server did not become HTTP-ready")
                    print(
                        "Installed server status, setup page and phone bridge passed",
                        flush=True,
                    )
                    ready = True
                    return True
                except urllib.error.HTTPError:
                    raise
                except (urllib.error.URLError, TimeoutError, ConnectionError):
                    time.sleep(0.1)
            raise TimeoutError("Installed server did not become HTTP-ready")
        finally:
            try:
                # A confirmed collision belongs to another process, not this
                # child's cleanup. Still check the other listener for leaks.
                _stop_server(
                    process,
                    tuple(p for p in (port, port + 1) if p != conflict),
                    interrupt=ready,
                )
                if ready and state_file.exists():
                    raise RuntimeError("Server state survived interrupt cleanup")
            finally:
                print(
                    log.read_text(encoding="utf-8", errors="replace"),
                    end="",
                    flush=True,
                )


def smoke_server(
    cli: Path, root: Path, env: dict[str, str], *, timeout: float = 60
) -> None:
    """Require real HTTP responses; retry only port collisions within one budget."""
    deadline = time.monotonic() + timeout
    used: set[int] = set()
    for attempt in range(1, 4):
        if time.monotonic() >= deadline:
            raise TimeoutError("Installed server did not become HTTP-ready")
        port = _smoke_port(used)
        used.add(port)
        print(
            f"Installed server attempt {attempt}/3: ports {port}, {port + 1}",
            flush=True,
        )
        if _smoke_server_attempt(
            cli, root, env, port=port, deadline=deadline, attempt=attempt
        ):
            print(
                "Installed server interrupt passed; state cleared and both listeners closed",
                flush=True,
            )
            return
        print(
            f"Installed server startup hit a port collision on attempt {attempt}",
            flush=True,
        )
    raise RuntimeError("Installed server could not start after 3 port collisions")


def smoke_installer(
    bundle: Path, *, upgrade: bool = False, log_dir: Path | None = None
) -> None:
    """Check a fresh install or a state-preserving upgrade to this run's wheel."""
    manifest = verify_local(bundle)
    baseline = manifest.get("upgrade_from")
    if upgrade:
        if not isinstance(baseline, str):
            raise ValueError("Missing upgrade baseline; record the bundle again")
        key = release.version_key(baseline)
        if key[3] != 3 or key >= release.version_key(manifest["version"]):
            raise ValueError(
                "Upgrade baseline must be a stable release older than the candidate"
            )
        print(f"Checking upgrade: {baseline} -> {manifest['version']}", flush=True)
    wheel = next(path for path in distributions(bundle) if path.suffix == ".whl")
    wheel_url = wheel.resolve().as_uri()
    repository = Path(__file__).resolve().parents[1]
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
    with tempfile.TemporaryDirectory(prefix="physiclaw-installer-") as temporary:
        root = Path(temporary)
        bin_dir = root / "bin"
        tool_dir = root / "tools"
        constraints = root / "constraints.txt"
        constraints.write_text(f"physiclaw @ {wheel_url}\n", encoding="utf-8")
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
        }
        env.update(
            PHYSICLAW_HOME=str(root / "home"),
            PHYSICLAW_VERSION=manifest["version"],
            PHYSICLAW_DRY_RUN="0",
            PHYSICLAW_DISABLE_UPDATE_CHECK="1",
            UV_TOOL_DIR=str(tool_dir),
            UV_TOOL_BIN_DIR=str(bin_dir),
            PHYSICLAW_PYTHON=python_version,
            # uv splits this setting on whitespace; a URI also supports spaces.
            UV_CONSTRAINT=constraints.as_uri(),
            UV_NO_CONFIG="1",
            UV_DEFAULT_INDEX="https://pypi.org/simple",
            PATH=str(bin_dir) + os.pathsep + env.get("PATH", ""),
            NO_COLOR="1",
            PYTHONUTF8="1",
            PYTHONIOENCODING="utf-8",
        )
        if os.name == "nt":
            # install.ps1 returns its status through LASTEXITCODE, without exiting
            # the user's shell. Propagate that status to the CI process.
            wrapper = root / "install.ps1"
            wrapper.write_text("& $args[0]\nexit $LASTEXITCODE\n", encoding="utf-8-sig")
            installer = [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(wrapper),
                str(repository / "install.ps1"),
            ]
        else:
            installer = ["bash", str(repository / "install.sh")]
        venv = tool_dir / "physiclaw"
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        cli = bin_dir / ("physiclaw.exe" if os.name == "nt" else "physiclaw")
        saved = {}
        if upgrade:
            baseline_env = {k: v for k, v in env.items() if k != "UV_CONSTRAINT"}
            baseline_env["PHYSICLAW_VERSION"] = baseline
            for command in (
                [
                    "uv",
                    "--no-config",
                    "tool",
                    "install",
                    f"physiclaw=={baseline}",
                    "--python",
                    python_version,
                ],
                [
                    str(python),
                    "-I",
                    "-c",
                    "import importlib.metadata as m, sys; "
                    "assert m.version('physiclaw') == sys.argv[1]",
                    baseline,
                ],
                [str(python), "-I", "-c", UPGRADE_SEED],
                [str(python), "-I", "-c", UPGRADE_CHECK],
            ):
                subprocess.run(
                    command, cwd=root, env=baseline_env, check=True, timeout=600
                )
            saved = {
                name: (root / "home" / name).read_bytes() for name in UPGRADE_FILES
            }
        probe = (
            "import importlib.metadata as m, json, sys; "
            "d = m.distribution('physiclaw'); "
            "assert d.version == sys.argv[1]; "
            "assert json.loads(d.read_text('direct_url.json'))['url'] == sys.argv[2]; "
            "assert sys.version_info[:2] == tuple(map(int, sys.argv[3].split('.')))"
        )
        for command in (
            installer,
            [
                str(python),
                "-I",
                "-c",
                probe,
                manifest["version"],
                wheel_url,
                python_version,
            ],
            ["uv", "--no-config", "pip", "check", "--python", str(python)],
            [str(cli), "doctor"],
        ):
            subprocess.run(command, cwd=root, env=env, check=True, timeout=600)
        if log_dir is not None:
            log_dir = log_dir.resolve()
            log_dir.mkdir(parents=True, exist_ok=True)
        smoke_server(cli, log_dir if log_dir is not None else root, env)
        if upgrade:
            subprocess.run(
                [str(python), "-I", "-c", UPGRADE_CHECK],
                cwd=root,
                env=env,
                check=True,
                timeout=60,
            )
            for name, original in saved.items():
                if (root / "home" / name).read_bytes() != original:
                    raise ValueError(f"Upgrade changed user data: {name}")
            print(
                f"Upgrade from {baseline} preserved and loaded user data",
                flush=True,
            )


def published_files(version: str) -> dict[str, dict]:
    try:
        with urllib.request.urlopen(
            f"https://pypi.org/pypi/physiclaw/{version}/json", timeout=30
        ) as response:
            return {item["filename"]: item for item in json.load(response)["urls"]}
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return {}
        raise


def wait_for_pypi_index(expected: dict[str, str], *, timeout: float = 300) -> None:
    """Wait for installation metadata, without retrying conflicting release files."""
    deadline = time.monotonic() + timeout
    reason = "release files are missing"
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            index = pypi_index(timeout=min(30, remaining))
        except urllib.error.HTTPError as error:
            if error.code not in {404, 408, 429, 500, 502, 503, 504}:
                raise
            reason = f"HTTP {error.code}"
        except (
            urllib.error.URLError,
            TimeoutError,
            ConnectionError,
            http.client.IncompleteRead,
        ) as error:
            reason = str(error)
        else:
            indexed = {item["filename"]: item for item in index["files"]}
            for name in expected.keys() & indexed.keys():
                item = indexed[name]
                if (
                    item["hashes"]["sha256"] != expected[name]
                    or item.get("yanked", False) is not False
                ):
                    raise ValueError(f"PyPI index file differs or is yanked: {name}")
            missing = expected.keys() - indexed.keys()
            if not missing and time.monotonic() < deadline:
                print("Verified PyPI installation index", flush=True)
                return
            reason = (
                "missing " + ", ".join(sorted(missing))
                if missing
                else "response arrived after the deadline"
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        delay = min(10, remaining)
        print(
            f"Waiting for PyPI installation index ({reason}); retry in {delay:g}s",
            flush=True,
        )
        time.sleep(delay)
    raise TimeoutError(
        f"PyPI installation index not ready after {timeout:g}s: {reason}"
    )


def verify_pypi(bundle: Path, *, allow_missing: bool = False) -> None:
    """Before retries, reject different existing files; after upload verify all bytes."""
    manifest = verify_local(bundle)
    expected = manifest["files"]
    for attempt in range(6 if not allow_missing else 1):
        published = published_files(manifest["version"])
        if set(published) - set(expected):
            raise ValueError("PyPI has unexpected files for this version")
        if allow_missing or set(published) == set(expected):
            break
        if attempt == 5:
            raise ValueError("PyPI does not have both release files yet")
        time.sleep(10)
    for name, item in published.items():
        if item["digests"]["sha256"] != expected[name] or item.get("yanked"):
            raise ValueError(f"Existing PyPI file differs or is yanked: {name}")
        url = item["url"]
        if not url.startswith("https://files.pythonhosted.org/"):
            raise ValueError("Unexpected PyPI file host")
        with urllib.request.urlopen(url, timeout=60) as response:
            actual = hashlib.file_digest(response, "sha256").hexdigest()
        if actual != expected[name]:
            raise ValueError(f"Downloaded PyPI file checksum mismatch: {name}")
        # The verifier downloads the file and validates its Sigstore provenance.
        # Existing files must already have provenance before skip-existing is safe.
        subprocess.run(
            [
                "uvx",
                "--from",
                "pypi-attestations==0.0.31",
                "pypi-attestations",
                "verify",
                "pypi",
                "--repository",
                REPOSITORY,
                url,
            ],
            check=True,
            timeout=180,
        )
        print(f"Verified PyPI bytes and attestations: {name}", flush=True)
    if not allow_missing:
        # The release JSON endpoint can be ready before installers see the files.
        wait_for_pypi_index(expected)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("record", "check", "smoke", "smoke-installer", "verify-pypi")
    )
    parser.add_argument("--bundle", type=Path, default=Path("release-bundle"))
    parser.add_argument("--allow-missing", action="store_true")
    parser.add_argument(
        "--upgrade",
        action="store_true",
        help="Start the installer check from the baseline recorded in the bundle",
    )
    parser.add_argument("--log-dir", type=Path, help="Keep installed-server smoke logs")
    args = parser.parse_args()
    if args.upgrade and args.action != "smoke-installer":
        parser.error("--upgrade requires smoke-installer")
    try:
        if args.action == "verify-pypi":
            verify_pypi(args.bundle, allow_missing=args.allow_missing)
        elif args.action == "smoke-installer":
            smoke_installer(
                args.bundle,
                upgrade=args.upgrade,
                log_dir=args.log_dir,
            )
        else:
            {
                "record": record,
                "check": verify_local,
                "smoke": smoke,
            }[args.action](args.bundle)
    except subprocess.CalledProcessError as error:
        if error.stdout:
            output = error.stdout
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            print(output, file=sys.stderr)
        raise SystemExit(f"Package check failed: {error}") from error


if __name__ == "__main__":
    main()
