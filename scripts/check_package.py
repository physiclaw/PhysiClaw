"""Check the exact distributions promoted by CI, without importing the checkout."""

import argparse
import hashlib
import json
import os
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

REPOSITORY = "https://github.com/physiclaw/PhysiClaw"
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
        "commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "files": {p.name: digest(p) for p in sorted(paths)},
    }
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
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


def _stop_server(process, port: int) -> None:
    windows = os.name == "nt"
    kill_tree = ["taskkill", "/PID", str(process.pid), "/T", "/F"]
    was_running = process.poll() is None
    try:
        if was_running:
            if windows:
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
            for number in (port, port + 1):
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
    if was_running and not windows and process.returncode not in (0, -signal.SIGTERM):
        raise RuntimeError(f"Server failed during shutdown: {process.returncode}")


def smoke_server(
    cli: Path, root: Path, env: dict[str, str], *, timeout: float = 60
) -> None:
    """Require the installed HTTP services to respond, then stop the server."""
    for _ in range(100):
        with socket.socket() as control, socket.socket() as bridge:
            control.bind(("127.0.0.1", 0))
            port = control.getsockname()[1]
            if port == 65535:
                continue
            try:
                bridge.bind(("0.0.0.0", port + 1))
            except OSError:
                continue
            break
    else:
        raise RuntimeError("No free adjacent ports for the server smoke test")

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
    log = root / "server.log"
    # Local probes must bypass any HTTP proxy inherited from the runner.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with log.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(
            command, cwd=root, env=env, stdout=output, stderr=subprocess.STDOUT
        )
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(
                        f"Server exited before HTTP readiness: {process.returncode}"
                    )
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
                    break
                except urllib.error.HTTPError:
                    raise
                except (urllib.error.URLError, TimeoutError, ConnectionError):
                    time.sleep(0.1)
            else:
                raise TimeoutError("Installed server did not become HTTP-ready")
        finally:
            try:
                _stop_server(process, port)
            finally:
                print(
                    log.read_text(encoding="utf-8", errors="replace"),
                    end="",
                    flush=True,
                )
    print("Installed server stopped; both listeners closed", flush=True)


def smoke_installer(bundle: Path) -> None:
    """Run the public installer, doctor and server against this run's wheel."""
    manifest = verify_local(bundle)
    wheel = next(path for path in distributions(bundle) if path.suffix == ".whl")
    wheel_url = wheel.resolve().as_uri()
    repository = Path(__file__).resolve().parents[1]
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
        probe = (
            "import importlib.metadata as m, json, sys; "
            "d = m.distribution('physiclaw'); "
            "assert d.version == sys.argv[1]; "
            "assert json.loads(d.read_text('direct_url.json'))['url'] == sys.argv[2]"
        )
        for command in (
            installer,
            [str(python), "-I", "-c", probe, manifest["version"], wheel_url],
            ["uv", "--no-config", "pip", "check", "--python", str(python)],
            [str(cli), "doctor"],
        ):
            subprocess.run(command, cwd=root, env=env, check=True, timeout=600)
        smoke_server(cli, root, env)


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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("record", "check", "smoke", "smoke-installer", "verify-pypi")
    )
    parser.add_argument("--bundle", type=Path, default=Path("release-bundle"))
    parser.add_argument("--allow-missing", action="store_true")
    args = parser.parse_args()
    try:
        if args.action == "verify-pypi":
            verify_pypi(args.bundle, allow_missing=args.allow_missing)
        else:
            {
                "record": record,
                "check": verify_local,
                "smoke": smoke,
                "smoke-installer": smoke_installer,
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
