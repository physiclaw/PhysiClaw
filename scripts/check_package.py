"""Check the exact distributions promoted by CI, without importing the checkout."""

import argparse
import hashlib
import json
import os
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
    "agent/trace/viewer.html",
    "studio/studio.html",
    "core/setup/static/setup-hardware.html",
    "core/setup/static/bridge.html",
    "cli/static/test-assistive-touch.html",
    "common/static/vendor/qrcode-1.4.4.min.js",
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
                    text=True,
                    encoding="utf-8",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=300,
                )
                print(result.stdout, end="", flush=True)
                return result.stdout

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


def smoke_installer(bundle: Path) -> None:
    """Run the public installer and doctor against this run's wheel."""
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
            UV_TOOL_DIR=str(tool_dir),
            UV_TOOL_BIN_DIR=str(bin_dir),
            # uv splits this setting on whitespace; a URI also supports spaces.
            UV_CONSTRAINT=constraints.as_uri(),
            UV_NO_CONFIG="1",
            UV_DEFAULT_INDEX="https://pypi.org/simple",
            PATH=str(bin_dir) + os.pathsep + env.get("PATH", ""),
            NO_COLOR="1",
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
            print(error.stdout, file=sys.stderr)
        raise SystemExit(f"Package check failed: {error}") from error


if __name__ == "__main__":
    main()
