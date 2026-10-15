"""Prepare, verify and publish hardware releases through one guarded path.

Preparation is local and builds in an empty output tree. Publishing accepts
only a verified bundle from the current clean, pushed main commit. Uploads
stay in a draft until the downloaded bytes match the local manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from html.parser import HTMLParser
from importlib.metadata import version as package_version
from pathlib import Path
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

from hardware.build_lock import inherited_fds, locked_main
from hardware.manual.build_extrusion_drawing import LANG_FILENAME as DRAWINGS
from hardware.manual.build_extrusion_drawing import PDF_FILENAME as DRAWING_PDFS
from hardware.manual.build_manual import LANG_FILENAME as MANUALS
from hardware.manual.build_sourcing_guide import LANG_FILENAME as SOURCING
from hardware.manual.pdf import find_chrome, render_pdf
from hardware.scheme import REPO_ROOT

# This location is deliberately independent of the subprocess output override.
RELEASE_DIR = REPO_ROOT / "hardware" / "output" / "release"
PRINT_ZIP = "physiclaw_custom_parts.zip"
FRAME_STEP = "camera_40_frame_assembled.step"
FRAME_ZIP = "physiclaw_camera_frame_assembled.zip"
MANUAL_ZIP = "physiclaw-assembly-manual.zip"
SOURCING_ZIP = "physiclaw-sourcing-guide.zip"
MANIFEST = "manifest.json"
ASSETS = frozenset(
    [PRINT_ZIP, FRAME_ZIP, MANUAL_ZIP, SOURCING_ZIP] + list(DRAWING_PDFS.values())
)


class ReleaseError(RuntimeError):
    pass


def validate_version(value: str) -> str:
    if not re.fullmatch(
        r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*))?", value
    ):
        raise ReleaseError("Set HW_VERSION to X.Y or X.Y.Z (numeric components only)")
    return value


def tag_for(version: str) -> str:
    return f"physiclaw-hardware-v{validate_version(version)}"


def run(*args: str) -> str:
    result = subprocess.run(
        args,
        cwd=REPO_ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        timeout=120,
    )
    return result.stdout.strip()


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source_digest() -> str:
    """Hash tracked AND untracked non-ignored inputs, including filenames."""
    names = run("git", "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    h = hashlib.sha256()
    for name in sorted(set(names.split("\0")) - {""}):
        path = REPO_ROOT / name
        if path.is_symlink() or not path.is_file():
            raise ReleaseError(f"Source is missing or a symlink: {name}")
        h.update(name.encode() + b"\0" + digest(path).encode() + b"\0")
    return h.hexdigest()


def _regular(path: Path) -> None:
    if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
        raise ReleaseError(f"Missing, empty or symlink artifact: {path}")


def _validate_bytes(name: str, data: bytes) -> None:
    if not data:
        raise ReleaseError(f"Empty artifact: {name}")
    suffix = Path(name).suffix
    if suffix == ".pdf" and not (
        data.startswith(b"%PDF-") and data.rstrip().endswith(b"%%EOF")
    ):
        raise ReleaseError(f"Incomplete PDF: {name}")
    if suffix == ".step" and not (
        data.lstrip().startswith(b"ISO-10303-21;")
        and data.rstrip().endswith(b"END-ISO-10303-21;")
    ):
        raise ReleaseError(f"Incomplete STEP: {name}")
    if suffix == ".svg":
        try:
            root = ElementTree.fromstring(data)
            if root.tag.split("}")[-1] != "svg":
                raise ValueError("not an SVG")
        except (ElementTree.ParseError, ValueError) as exc:
            raise ReleaseError(f"Invalid SVG: {name}: {exc}") from exc
    if suffix == ".html" and b"</html>" not in data.lower():
        raise ReleaseError(f"Incomplete HTML: {name}")


class _AssetReferences(HTMLParser):
    def __init__(self):
        super().__init__()
        self.refs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in {"img", "script", "link"}:
            for name, value in attrs:
                if name in {"src", "href"} and value:
                    self.refs.append(value)


def _validate_members(members: dict[str, bytes]) -> None:
    for name, data in members.items():
        path = Path(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name:
            raise ReleaseError(f"Unsafe archive path: {name}")
        _validate_bytes(name, data)
        if path.suffix == ".html":
            parser = _AssetReferences()
            parser.feed(data.decode("utf-8"))
            for ref in parser.refs:
                url = urlsplit(ref)
                if url.scheme == "data":
                    continue
                if url.scheme or url.netloc:
                    raise ReleaseError(
                        f"Offline document depends on remote asset: {ref}"
                    )
                target = path.parent / unquote(url.path)
                if (
                    target.is_absolute()
                    or ".." in target.parts
                    or target.as_posix() not in members
                ):
                    raise ReleaseError(
                        f"Missing or unsafe document asset: {name}: {ref}"
                    )


def _read_zip(path: Path) -> dict[str, bytes]:
    _regular(path)
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            if len(names) != len(set(names)):
                raise ReleaseError(f"Duplicate archive entries: {path.name}")
            members = {name: zf.read(name) for name in names}
    except zipfile.BadZipFile as exc:
        raise ReleaseError(f"Corrupt archive: {path.name}") from exc
    _validate_members(members)
    return members


def _required_documents(mapping: dict[str, str], directory: str, pdf: bool) -> set[str]:
    names = {f"{directory}/{name}" for name in mapping.values()}
    if pdf:
        names |= {str(Path(name).with_suffix(".pdf")) for name in names}
    return names


def validate_assets(bundle: Path) -> None:
    for name in ASSETS:
        path = bundle / name
        _regular(path)
        if path.suffix == ".pdf":
            _validate_bytes(name, path.read_bytes())
    print_parts = _read_zip(bundle / PRINT_ZIP)
    guides = {"3d_printed_parts_guide.txt", "3d打印零件说明.txt"}
    if not guides.issubset(print_parts) or not any(
        n.endswith(".step") for n in print_parts
    ):
        raise ReleaseError("Print package is missing its parts or bilingual guides")
    frame = _read_zip(bundle / FRAME_ZIP)
    if set(frame) != {FRAME_STEP}:
        raise ReleaseError(
            "Camera-frame archive must contain exactly its assembled STEP"
        )
    manual = _read_zip(bundle / MANUAL_ZIP)
    sourcing = _read_zip(bundle / SOURCING_ZIP)
    if not _required_documents(MANUALS, "manual", True).issubset(manual):
        raise ReleaseError("Manual must include HTML and PDF in both languages")
    required = _required_documents(SOURCING, "sourcing", False)
    required |= _required_documents(DRAWINGS, "drawing", True)
    if not required.issubset(sourcing):
        raise ReleaseError("Sourcing package is missing a guide or extrusion drawing")
    for pdf in DRAWING_PDFS.values():
        if sourcing[f"drawing/{pdf}"] != (bundle / pdf).read_bytes():
            raise ReleaseError(f"Direct and bundled extrusion drawings differ: {pdf}")


def _zip(path: Path, root: Path, files: list[Path]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in sorted(files):
            _regular(file)
            # Stable zip metadata; repeated packaging doesn't change hashes.
            info = zipfile.ZipInfo(
                file.relative_to(root).as_posix(), (1980, 1, 1, 0, 0, 0)
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            zf.writestr(info, file.read_bytes())


def _stage(output: Path, *args: str) -> None:
    env = {**os.environ, "PHYSICLAW_HW_OUTPUT": str(output)}
    subprocess.run(
        [sys.executable, "-m", "hardware", *args],
        cwd=REPO_ROOT,
        env=env,
        pass_fds=inherited_fds(),
        check=True,
    )


def run_tests() -> None:
    subprocess.run(
        [sys.executable, "-m", "pytest", "tests/hardware", "-q"],
        cwd=REPO_ROOT,
        check=True,
        pass_fds=inherited_fds(),
        timeout=600,
    )


def prepare(version: str) -> Path:
    tag = tag_for(version)
    destination = RELEASE_DIR / tag
    if destination.exists():
        raise ReleaseError(
            f"Bundle already exists: {destination}; verify it or choose a new version"
        )
    chrome = find_chrome()
    if chrome is None:
        raise ReleaseError(
            "Release preparation requires Chrome/Chromium for both PDF editions"
        )
    # Presence is insufficient: browser launch permissions or a broken install
    # can prevent printing. Detect that before spending time on CAD rendering.
    with tempfile.TemporaryDirectory(prefix="physiclaw-pdf-preflight-") as tmp:
        if not render_pdf(
            "<!DOCTYPE html><html><body>Hardware PDF preflight</body></html>",
            Path(tmp) / "probe.pdf",
            chrome,
        ):
            raise ReleaseError(
                "Chrome could not render the preflight PDF; check browser launch permissions"
            )
    sources = source_digest()
    commit = run("git", "rev-parse", "HEAD")
    run_tests()
    RELEASE_DIR.mkdir(parents=True, exist_ok=True)
    # Never bless an interactive output/ tree. No cache is read for releases.
    with tempfile.TemporaryDirectory(prefix=".prepare-", dir=RELEASE_DIR) as tmp:
        work = Path(tmp)
        output, bundle = work / "output", work / "bundle"
        bundle.mkdir()
        for args in (
            ("check",),
            ("build", "--bom", "--no-cache"),
            ("print",),
            ("manual", "--pdf"),
            ("sourcing",),
            ("drawing", "--pdf"),
            ("check",),
        ):
            _stage(output, *args)
        shutil.copyfile(output / "print_3d" / PRINT_ZIP, bundle / PRINT_ZIP)
        _zip(bundle / FRAME_ZIP, output / "step", [output / "step" / FRAME_STEP])
        for name, directories in (
            (MANUAL_ZIP, ["manual"]),
            (SOURCING_ZIP, ["sourcing", "drawing"]),
        ):
            files = [
                p for d in directories for p in (output / d).rglob("*") if p.is_file()
            ]
            _zip(bundle / name, output, files)
        for pdf in DRAWING_PDFS.values():
            shutil.copyfile(output / "drawing" / pdf, bundle / pdf)
        validate_assets(bundle)
        if source_digest() != sources or run("git", "rev-parse", "HEAD") != commit:
            raise ReleaseError(
                "Sources changed during the build; discard and prepare again"
            )
        manifest = {
            "schema": 1,
            "version": version,
            "commit": commit,
            "sources": sources,
            "toolchain": {
                name: package_version(name)
                for name in ("build123d", "cadquery-ocp", "shapely")
            },
            "python": sys.version,
            "artifacts": {name: digest(bundle / name) for name in sorted(ASSETS)},
        }
        (bundle / MANIFEST).write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        bundle.rename(destination)
    print(f"Prepared and verified local release: {destination}")
    return destination


def verify(version: str) -> tuple[Path, dict]:
    bundle = RELEASE_DIR / tag_for(version)
    _regular(bundle / MANIFEST)
    try:
        manifest = json.loads((bundle / MANIFEST).read_text())
    except (ValueError, OSError) as exc:
        raise ReleaseError(f"Unreadable release manifest: {exc}") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema") != 1
        or manifest.get("version") != version
    ):
        raise ReleaseError("Wrong release manifest schema or version")
    if (
        manifest.get("commit") != run("git", "rev-parse", "HEAD")
        or manifest.get("sources") != source_digest()
    ):
        raise ReleaseError(
            "Bundle was built from different sources; prepare a new bundle"
        )
    hashes = manifest.get("artifacts")
    if not isinstance(hashes, dict) or set(hashes) != ASSETS:
        raise ReleaseError(
            "Manifest does not contain exactly the required release assets"
        )
    if {p.name for p in bundle.iterdir()} != ASSETS | {MANIFEST}:
        raise ReleaseError("Bundle contains missing or unexpected files")
    for name, sha in hashes.items():
        _regular(bundle / name)
        if digest(bundle / name) != sha:
            raise ReleaseError(f"Artifact changed after preparation: {name}")
    validate_assets(bundle)
    return bundle, manifest


def _git_state(version: str) -> tuple[str, str, str]:
    tag = tag_for(version)
    if run("git", "branch", "--show-current") != "main":
        raise ReleaseError("Hardware publishing requires the main branch")
    if run("git", "status", "--porcelain", "--untracked-files=all"):
        raise ReleaseError("Working tree is dirty; commit and push before publishing")
    commit = run("git", "rev-parse", "HEAD")
    refs = run(
        "git",
        "ls-remote",
        "origin",
        "refs/heads/main",
        f"refs/tags/{tag}",
        f"refs/tags/{tag}^{{}}",
    )
    remote = dict(line.split()[::-1] for line in refs.splitlines())
    if remote.get("refs/heads/main") != commit:
        raise ReleaseError(
            "HEAD differs from live origin/main; push or update main first"
        )
    if any(name.startswith("refs/tags/") for name in remote):
        raise ReleaseError(f"Remote tag already exists: {tag}")
    if run("git", "tag", "--list", tag):
        raise ReleaseError(f"Local tag already exists: {tag}")
    origin = run("git", "remote", "get-url", "origin")
    match = re.fullmatch(
        r"(?:git@([^:]+):|https://([^/]+)/)([^/]+/[^/]+?)(?:\.git)?", origin
    )
    if not match:
        raise ReleaseError("origin must be a GitHub SSH or HTTPS repository URL")
    host, slug = match[1] or match[2], match[3]
    return commit, host, slug


def preflight(version: str) -> tuple[str, str]:
    commit, host, slug = _git_state(version)
    # A failed auth/network request is an error, never evidence of absence.
    tags = run(
        "gh",
        "api",
        "--hostname",
        host,
        f"repos/{slug}/releases?per_page=100",
        "--paginate",
        "--jq",
        ".[].tag_name",
    )
    if tag_for(version) in tags.splitlines():
        raise ReleaseError(
            f"Release or draft already exists: {tag_for(version)}; inspect it before retrying"
        )
    return commit, f"{host}/{slug}"


def publish(version: str) -> None:
    bundle, manifest = verify(version)
    commit, repo = preflight(version)
    tag = tag_for(version)
    names = sorted(ASSETS | {MANIFEST})
    run(
        "gh",
        "release",
        "create",
        tag,
        "--repo",
        repo,
        "--draft",
        "--target",
        commit,
        "--title",
        f"PhysiClaw hardware v{version}",
        "--notes",
        f"Bilingual assembly manual, sourcing guide, extrusion drawings and custom STEP parts.\n\nSource commit: {commit}\nArtifact SHA-256 checksums and build provenance: manifest.json.",
    )
    # On failure, leave the draft for inspection; never delete or overwrite it.
    run(
        "gh",
        "release",
        "upload",
        tag,
        *[str(bundle / name) for name in names],
        "--repo",
        repo,
    )
    remote = json.loads(
        run(
            "gh",
            "release",
            "view",
            tag,
            "--repo",
            repo,
            "--json",
            "isDraft,targetCommitish,assets",
        )
    )
    if not remote["isDraft"] or remote["targetCommitish"] != commit:
        raise ReleaseError("Draft state or target changed; publication stopped")
    remote_names = [asset["name"] for asset in remote["assets"]]
    if sorted(remote_names) != names:
        raise ReleaseError("Draft asset set differs from the verified bundle")
    with tempfile.TemporaryDirectory(prefix="physiclaw-release-check-") as tmp:
        run("gh", "release", "download", tag, "--repo", repo, "--dir", tmp)
        for name in names:
            downloaded = Path(tmp) / name
            _regular(downloaded)
            if digest(downloaded) != digest(bundle / name):
                raise ReleaseError(f"Uploaded artifact checksum mismatch: {name}")
    # Re-check local inputs and live remote HEAD after the potentially long upload.
    _, current = verify(version)
    now, host, slug = _git_state(version)
    if current != manifest or now != commit or f"{host}/{slug}" != repo:
        raise ReleaseError("Release inputs changed during upload")
    run(
        "gh",
        "release",
        "edit",
        tag,
        "--repo",
        repo,
        "--draft=false",
        "--latest",
        "--target",
        commit,
    )
    print(
        f"Published {tag} at {commit}. Run make docs-deploy to refresh the docs site."
    )


@locked_main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("prepare", "verify", "preflight", "publish", "deploy")
    )
    parser.add_argument("--version", default=os.environ.get("HW_VERSION", ""))
    args = parser.parse_args(argv)
    try:
        version = validate_version(args.version)
        if args.action == "deploy":
            preflight(version)
            prepare(version)
            publish(version)
        else:
            {
                "prepare": prepare,
                "verify": verify,
                "preflight": preflight,
                "publish": publish,
            }[args.action](version)
    except (ReleaseError, subprocess.SubprocessError, OSError, ValueError) as exc:
        print(f"Hardware release stopped: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
