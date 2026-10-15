"""Prepare, verify and publish hardware releases through one guarded path.

Preparation uses validated CAD cache entries in a private output tree. Publishing
accepts only a verified bundle from the current clean, pushed main commit. Uploads
stay in a draft until their SHA-256 checksums match the local bundle.
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
from hardware.scheme import CACHE_DIR, REPO_ROOT

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


def run(*args: str, timeout: int = 120) -> str:
    result = subprocess.run(
        args,
        cwd=REPO_ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        timeout=timeout,
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
        verify(version)
        print(f"Reusing verified local release: {destination}")
        return destination
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
    # Share only the validated cache; the CLI holds its output tree lock.
    # All restored/generated artifacts and packaging remain private.
    with tempfile.TemporaryDirectory(prefix=".prepare-", dir=RELEASE_DIR) as tmp:
        work = Path(tmp)
        output, bundle = work / "output", work / "bundle"
        bundle.mkdir()
        output.mkdir()
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (output / ".cache").symlink_to(CACHE_DIR.resolve(), target_is_directory=True)
        for args in (
            ("check",),
            ("build", "--bom"),
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


def _release(repo: str, tag: str, release_id: int | None = None) -> dict | None:
    host, slug = repo.split("/", 1)
    args = ["gh", "api", "--hostname", host]
    if release_id is None:
        # The by-tag endpoint does not find unpublished drafts. A failed
        # auth/network request must remain an error, never evidence of absence.
        args += [
            f"repos/{slug}/releases?per_page=100",
            "--paginate",
            "--jq",
            f'.[] | select(.tag_name == "{tag}")',
        ]
    else:
        args += [f"repos/{slug}/releases/{release_id}"]
    result = run(*args)
    return json.loads(result) if result else None


def _check_draft(remote: dict, tag: str, commit: str) -> None:
    if (
        remote.get("draft") is not True
        or remote.get("tag_name") != tag
        or remote.get("target_commitish") != commit
    ):
        raise ReleaseError(
            "Existing release must be an unpublished draft of this exact commit; "
            "inspect it before retrying"
        )


def preflight(version: str) -> tuple[str, str, dict | None]:
    commit, host, slug = _git_state(version)
    repo, tag = f"{host}/{slug}", tag_for(version)
    remote = _release(repo, tag)
    if remote is not None:
        _check_draft(remote, tag, commit)
    return commit, repo, remote


def _check_assets(remote: dict, bundle: Path, *, complete: bool = False) -> dict:
    assets = {asset["name"]: asset for asset in remote["assets"]}
    expected = ASSETS | {MANIFEST}
    if (
        len(assets) != len(remote["assets"])
        or not assets.keys() <= expected
        or (complete and assets.keys() != expected)
    ):
        raise ReleaseError("Draft asset set differs from the verified bundle")
    for name, asset in assets.items():
        if (
            asset["state"] != "uploaded"
            or asset["size"] != (bundle / name).stat().st_size
        ):
            raise ReleaseError(
                f"Draft artifact is incomplete or has the wrong size: {name}"
            )
        if asset.get("digest") and asset["digest"] != f"sha256:{digest(bundle / name)}":
            raise ReleaseError(f"Uploaded artifact checksum mismatch: {name}")
    return assets


def _asset_signature(remote: dict) -> list[tuple]:
    # download_count changes during fallback verification; content identity must not.
    return sorted(
        tuple(
            asset.get(key)
            for key in ("name", "id", "size", "state", "digest", "updated_at")
        )
        for asset in remote["assets"]
    )


def publish(version: str) -> None:
    bundle, manifest = verify(version)
    commit, repo, remote = preflight(version)
    tag = tag_for(version)
    if remote is None:
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
        remote = _release(repo, tag)
    if remote is None:
        raise ReleaseError(
            "Created draft could not be found; inspect it before retrying"
        )
    _check_draft(remote, tag, commit)
    assets = _check_assets(remote, bundle)
    missing = sorted((ASSETS | {MANIFEST}) - assets.keys())
    release_id = remote["id"]
    if missing:
        print(
            f"Uploading {len(missing)} missing assets; retaining {len(assets)} uploaded assets",
            flush=True,
        )
        # On failure, leave the draft. Retrying uploads only missing assets.
        run(
            "gh",
            "release",
            "upload",
            tag,
            *[str(bundle / name) for name in missing],
            "--repo",
            repo,
            timeout=600,
        )
        remote = _release(repo, tag, release_id)
        if remote is None:
            raise ReleaseError("Draft disappeared during upload")
        _check_draft(remote, tag, commit)
        assets = _check_assets(remote, bundle, complete=True)
    fallback = [name for name, asset in assets.items() if not asset.get("digest")]
    if fallback:
        # Older GitHub servers may omit SHA-256. Download only those assets.
        with tempfile.TemporaryDirectory(prefix="physiclaw-release-check-") as tmp:
            for name in fallback:
                run(
                    "gh",
                    "release",
                    "download",
                    tag,
                    "--repo",
                    repo,
                    "--pattern",
                    name,
                    "--dir",
                    tmp,
                    timeout=600,
                )
                downloaded = Path(tmp) / name
                _regular(downloaded)
                if digest(downloaded) != digest(bundle / name):
                    raise ReleaseError(f"Uploaded artifact checksum mismatch: {name}")
    print(
        f"Verified {len(assets)} asset checksums ({len(fallback)} downloads)",
        flush=True,
    )
    # Re-check source, live origin/main and draft content after transfers.
    _, current = verify(version)
    now, host, slug = _git_state(version)
    if current != manifest or now != commit or f"{host}/{slug}" != repo:
        raise ReleaseError("Release inputs changed during upload")
    latest = _release(repo, tag, release_id)
    if latest is None:
        raise ReleaseError("Draft disappeared before publication")
    _check_draft(latest, tag, commit)
    if latest["id"] != release_id or _asset_signature(latest) != _asset_signature(
        remote
    ):
        raise ReleaseError(
            "Draft assets changed during verification; retry after inspection"
        )
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
    print(f"Published {tag} at {commit}", flush=True)
    try:
        print(run("make", "docs-deploy"), flush=True)
    except (subprocess.SubprocessError, OSError) as exc:
        raise ReleaseError(
            "Hardware release is published, but the docs hook failed. "
            "Retry only: make docs-deploy"
        ) from exc


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
