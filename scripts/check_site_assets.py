#!/usr/bin/env python3
"""Compare the explicit site file list with current GitHub sources/releases.

Run with Python 3.12+, without installing PhysiClaw or cloning either site repo.
The file inventory is embedded below. Run with --report /tmp/site-assets.csv
to export every file and recursive ZIP member; --list shows the inventory.
Requires network access; GITHUB_TOKEN/GH_TOKEN is optional for API rate limits.
Unlinked server leftovers require a deployment manifest to enumerate.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import textwrap
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, fields, replace
from html.parser import HTMLParser
from http.client import HTTPException
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

REPO = "physiclaw/PhysiClaw"
API = "https://api.github.com"
RELEASES = f"https://github.com/{REPO}/releases/download"
HARDWARE_PREFIX = "physiclaw-hardware-v"
FAVICONS = (
    '<link rel="icon" href="/favicon.ico" sizes="32x32">'
    '<link rel="icon" href="/favicon.svg" type="image/svg+xml">'
)


@dataclass(frozen=True, kw_only=True)
class Asset:
    file: str
    web_url: str
    github_source_url: str
    release_asset_url: str = ""
    check: str = "source"
    member: str = ""
    part: int | None = None
    rewrite_downloads: bool = False
    prefix: str = ""


@dataclass(frozen=True, kw_only=True)
class Inventory:
    files: tuple[Asset, ...]
    expand: tuple[Asset, ...]
    rewrite_downloads: tuple[str, ...]
    public_repositories: dict[str, str]
    hardware_tag: str = ""

    def for_hardware(self, tag: str) -> Inventory:
        def resolve(asset: Asset) -> Asset:
            return replace(
                asset,
                release_asset_url=asset.release_asset_url.replace(
                    "{hardware_tag}", tag
                ),
            )

        return replace(
            self,
            hardware_tag=tag,
            files=tuple(resolve(asset) for asset in self.files),
            expand=tuple(resolve(asset) for asset in self.expand),
        )


@dataclass(frozen=True)
class CheckResult:
    file: str
    web_url: str
    github_source_url: str
    release_asset_url: str
    archive_member: str
    release_member: str
    status: str
    detail: str


# Stable served filenames and their GitHub references. Release members and gallery
# images expand from the newest archives so their complete file lists stay current.
INVENTORY = Inventory(
    files=(
        Asset(
            file="install.sh",
            web_url="https://physiclaw.ai/install.sh",
            github_source_url="https://github.com/physiclaw/PhysiClaw/blob/main/install.sh",
        ),
        Asset(
            file="install.ps1",
            web_url="https://physiclaw.ai/install.ps1",
            github_source_url="https://github.com/physiclaw/PhysiClaw/blob/main/install.ps1",
        ),
        *(
            Asset(
                file=name,
                web_url=f"https://physiclaw.ai/{name}",
                github_source_url=f"https://github.com/physiclaw/PhysiClaw-site/blob/main/public/{name}",
            )
            for name in ("arm-phone.svg", "crab.svg", "favicon.ico", "favicon.svg")
        ),
        Asset(
            file="downloads/firmware/fluidnc.zip",
            web_url="https://physiclaw.ai/downloads/firmware/fluidnc.zip",
            github_source_url="https://github.com/physiclaw/PhysiClaw/blob/main/firmware/firmware.py",
            release_asset_url=f"{RELEASES}/firmware_fluidNC/fluidnc.zip",
            check="release",
        ),
        *(
            Asset(
                file=f"downloads/local_vision_model.zip.b64.{part:02d}",
                web_url=f"https://physiclaw.ai/downloads/local_vision_model.zip.b64.{part:02d}",
                github_source_url="https://github.com/physiclaw/PhysiClaw-site/blob/main/scripts/fetch-downloads.mjs",
                release_asset_url=f"{RELEASES}/local-vision-model/local_vision_model.zip",
                check="vision",
                part=part,
            )
            for part in range(4)
        ),
        Asset(
            file="downloads/official-skills/physiclaw_official_skills.zip",
            web_url="https://physiclaw.ai/downloads/official-skills/physiclaw_official_skills.zip",
            github_source_url="https://github.com/physiclaw/PhysiClaw/tree/main/skills",
            check="skills",
        ),
        Asset(
            file="downloads/official-skills/physiclaw_official_skills.zip.sha256",
            web_url="https://physiclaw.ai/downloads/official-skills/physiclaw_official_skills.zip.sha256",
            github_source_url="https://github.com/physiclaw/PhysiClaw/tree/main/skills",
            check="skills-checksum",
        ),
        Asset(
            file="downloads/official-skills/latest.json",
            web_url="https://physiclaw.ai/downloads/official-skills/latest.json",
            github_source_url="https://github.com/physiclaw/PhysiClaw/tree/main/skills",
            check="skills-latest",
        ),
        Asset(
            file=".release-version",
            web_url="https://docs.physiclaw.ai/.release-version",
            github_source_url="https://github.com/physiclaw/docs-site/blob/main/scripts/fetch-release.mjs",
            check="release-tag",
        ),
        Asset(
            file="favicon.ico",
            web_url="https://docs.physiclaw.ai/favicon.ico",
            github_source_url="https://github.com/physiclaw/docs-site/blob/main/public/favicon.ico",
        ),
        Asset(
            file="favicon.svg",
            web_url="https://docs.physiclaw.ai/favicon.svg",
            github_source_url="https://github.com/physiclaw/docs-site/blob/main/public/favicon.svg",
        ),
        Asset(
            file="downloads/physiclaw_manual.pdf",
            web_url="https://docs.physiclaw.ai/downloads/physiclaw_manual.pdf",
            github_source_url="https://github.com/physiclaw/PhysiClaw/blob/main/hardware/documents/manual/build.py",
            release_asset_url=f"{RELEASES}/{{hardware_tag}}/physiclaw-assembly-manual.zip",
            check="member",
            member="manual/physiclaw_manual.pdf",
        ),
        Asset(
            file="downloads/physiclaw装配手册.pdf",
            web_url="https://docs.physiclaw.ai/downloads/physiclaw装配手册.pdf",
            github_source_url="https://github.com/physiclaw/PhysiClaw/blob/main/hardware/documents/manual/build.py",
            release_asset_url=f"{RELEASES}/{{hardware_tag}}/physiclaw-assembly-manual.zip",
            check="member",
            member="manual/physiclaw装配手册.pdf",
        ),
        Asset(
            file="downloads/physiclaw_custom_parts.zip",
            web_url="https://docs.physiclaw.ai/downloads/physiclaw_custom_parts.zip",
            github_source_url="https://github.com/physiclaw/PhysiClaw/blob/main/hardware/release.py",
            release_asset_url=f"{RELEASES}/{{hardware_tag}}/physiclaw_custom_parts.zip",
            check="release",
        ),
        Asset(
            file="downloads/physiclaw_assembly_3d.zip",
            web_url="https://docs.physiclaw.ai/downloads/physiclaw_assembly_3d.zip",
            github_source_url="https://github.com/physiclaw/PhysiClaw/blob/main/hardware/release.py",
            release_asset_url=f"{RELEASES}/{{hardware_tag}}/physiclaw_camera_frame_assembled.zip",
            check="repack",
        ),
        Asset(
            file="downloads/physiclaw_extrusion_drawing_en.pdf",
            web_url="https://docs.physiclaw.ai/downloads/physiclaw_extrusion_drawing_en.pdf",
            github_source_url="https://github.com/physiclaw/PhysiClaw/blob/main/hardware/documents/drawing/extrusion.py",
            release_asset_url=f"{RELEASES}/{{hardware_tag}}/physiclaw_extrusion_drawing_en.pdf",
            check="release",
        ),
        Asset(
            file="downloads/physiclaw_extrusion_drawing_zh.pdf",
            web_url="https://docs.physiclaw.ai/downloads/physiclaw_extrusion_drawing_zh.pdf",
            github_source_url="https://github.com/physiclaw/PhysiClaw/blob/main/hardware/documents/drawing/extrusion.py",
            release_asset_url=f"{RELEASES}/{{hardware_tag}}/physiclaw_extrusion_drawing_zh.pdf",
            check="release",
        ),
        Asset(
            file="en/hardware/manual/index.html",
            web_url="https://docs.physiclaw.ai/en/hardware/manual/index.html",
            github_source_url="https://github.com/physiclaw/PhysiClaw/blob/main/hardware/documents/manual/build.py",
            release_asset_url=f"{RELEASES}/{{hardware_tag}}/physiclaw-assembly-manual.zip",
            check="html",
            member="manual/physiclaw_manual.html",
        ),
        Asset(
            file="en/hardware/sourcing-guide/index.html",
            web_url="https://docs.physiclaw.ai/en/hardware/sourcing-guide/index.html",
            github_source_url="https://github.com/physiclaw/PhysiClaw/blob/main/hardware/documents/sourcing/build.py",
            release_asset_url=f"{RELEASES}/{{hardware_tag}}/physiclaw-sourcing-guide.zip",
            check="html",
            member="sourcing/sourcing_guide.html",
            rewrite_downloads=True,
        ),
        Asset(
            file="en/hardware/extrusion-drawing/index.html",
            web_url="https://docs.physiclaw.ai/en/hardware/extrusion-drawing/index.html",
            github_source_url="https://github.com/physiclaw/PhysiClaw/blob/main/hardware/documents/drawing/extrusion.py",
            release_asset_url=f"{RELEASES}/{{hardware_tag}}/physiclaw-sourcing-guide.zip",
            check="html",
            member="drawing/physiclaw_extrusion_drawing_en.html",
        ),
        Asset(
            file="zh/hardware/manual/index.html",
            web_url="https://docs.physiclaw.ai/zh/hardware/manual/index.html",
            github_source_url="https://github.com/physiclaw/PhysiClaw/blob/main/hardware/documents/manual/build.py",
            release_asset_url=f"{RELEASES}/{{hardware_tag}}/physiclaw-assembly-manual.zip",
            check="html",
            member="manual/physiclaw装配手册.html",
        ),
        Asset(
            file="zh/hardware/sourcing-guide/index.html",
            web_url="https://docs.physiclaw.ai/zh/hardware/sourcing-guide/index.html",
            github_source_url="https://github.com/physiclaw/PhysiClaw/blob/main/hardware/documents/sourcing/build.py",
            release_asset_url=f"{RELEASES}/{{hardware_tag}}/physiclaw-sourcing-guide.zip",
            check="html",
            member="sourcing/physiclaw采购指南.html",
            rewrite_downloads=True,
        ),
        Asset(
            file="zh/hardware/extrusion-drawing/index.html",
            web_url="https://docs.physiclaw.ai/zh/hardware/extrusion-drawing/index.html",
            github_source_url="https://github.com/physiclaw/PhysiClaw/blob/main/hardware/documents/drawing/extrusion.py",
            release_asset_url=f"{RELEASES}/{{hardware_tag}}/physiclaw-sourcing-guide.zip",
            check="html",
            member="drawing/physiclaw_extrusion_drawing_zh.html",
        ),
    ),
    expand=(
        Asset(
            file="crab.svg",
            web_url="https://docs.physiclaw.ai/{locale}/",
            github_source_url="https://github.com/physiclaw/docs-site/blob/main/src/assets/crab.svg",
            check="source-image",
        ),
        *(
            Asset(
                file=f"{locale}/hardware/manual/assets/",
                web_url=f"https://docs.physiclaw.ai/{locale}/hardware/manual/assets/",
                github_source_url="https://github.com/physiclaw/PhysiClaw/blob/main/hardware/documents/manual/build.py",
                release_asset_url=f"{RELEASES}/{{hardware_tag}}/physiclaw-assembly-manual.zip",
                check="members",
                prefix="assets/",
            )
            for locale in ("en", "zh")
        ),
        Asset(
            file="{locale}/hardware-gallery/",
            web_url="https://docs.physiclaw.ai/{locale}/hardware-gallery/",
            github_source_url="https://github.com/physiclaw/docs-site/blob/main/src/components/HardwareGallery.astro",
            release_asset_url=f"{RELEASES}/physiclaw-hardware-gallery/physiclaw_hardware_gallery.zip",
            check="gallery",
        ),
    ),
    rewrite_downloads=(
        "physiclaw_custom_parts.zip",
        "physiclaw_extrusion_drawing_en.pdf",
        "physiclaw_extrusion_drawing_zh.pdf",
    ),
    public_repositories={
        "physiclaw/PhysiClaw-site": "https://physiclaw.ai",
        "physiclaw/docs-site": "https://docs.physiclaw.ai",
    },
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def file_sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def blob(data):
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


class Redirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected and urlsplit(req.full_url).netloc != urlsplit(newurl).netloc:
            redirected.remove_header("Authorization")
        return redirected


class Downloads:
    def __init__(self, directory, timeout):
        self.directory = directory
        self.timeout = timeout

    def get(self, url):
        url = quote(url, safe="/:?=&%+@")
        path = self.directory / sha(url.encode())
        if path.exists():
            return path
        headers = {"User-Agent": "physiclaw-asset-check", "Accept-Encoding": "identity"}
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if urlsplit(url).netloc == "api.github.com" and token:
            headers["Authorization"] = f"Bearer {token}"
        # Each call owns its opener and temporary file, including parallel calls.
        pending = path.with_suffix(".part")
        for attempt in range(3):
            try:
                started = time.monotonic()
                with (
                    build_opener(Redirects).open(
                        Request(url, headers=headers), timeout=self.timeout
                    ) as response,
                    pending.open("wb") as output,
                ):
                    require(response.status == 200, f"HTTP {response.status}: {url}")
                    while chunk := response.read(256 * 1024):
                        output.write(chunk)
                        require(
                            time.monotonic() - started < self.timeout * 10,
                            f"download exceeded {self.timeout * 10:g}s: {url}",
                        )
                    size = response.headers.get("Content-Length")
                    require(
                        size is None or output.tell() == int(size),
                        f"truncated response: {url}",
                    )
                require(pending.stat().st_size > 0, f"empty response: {url}")
                pending.replace(path)
                return path
            except (OSError, ValueError, HTTPException) as exc:
                pending.unlink(missing_ok=True)
                retryable = not isinstance(exc, HTTPError) or exc.code in {
                    408,
                    429,
                    500,
                    502,
                    503,
                    504,
                }
                if attempt == 2 or not retryable:
                    raise
                time.sleep(attempt + 1)

    def json(self, url):
        return json.loads(self.get(url).read_bytes())


def archive_files(source, prefix="", depth=0):
    """Exact file names and leaf digests, including recursively nested ZIPs."""
    require(depth < 10, "nested ZIP depth exceeds 10")
    files = {}
    # Older release ZIPs used UTF-8 names without setting the ZIP UTF-8 flag.
    with zipfile.ZipFile(source, metadata_encoding="utf-8") as archive:
        for entry in archive.infolist():
            if entry.is_dir():
                continue
            name = entry.filename
            require(
                not PurePosixPath(name).is_absolute()
                and ".." not in PurePosixPath(name).parts
                and "\\" not in name,
                f"unsafe ZIP filename: {name!r}",
            )
            key = prefix + name
            require(key not in files, f"duplicate ZIP filename: {key}")
            if name.lower().endswith(".zip"):
                data = archive.read(entry)
                files[key] = sha(data)
                files.update(archive_files(io.BytesIO(data), key + "!/", depth + 1))
            else:
                with archive.open(entry) as stream:
                    files[key] = hashlib.file_digest(stream, "sha256").hexdigest()
    require(files, "empty ZIP archive")
    return files


class References(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.refs = set()
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        for key in ("href", "src"):
            if values.get(key):
                self.refs.add(values[key])
        for candidate in values.get("srcset", "").split(","):
            if parts := candidate.split():
                self.refs.add(parts[0])


class Terminal:
    def __init__(self, quiet=False, verbose=False):
        self.quiet = quiet
        self.verbose = verbose
        self.interactive = sys.stdout.isatty() and os.environ.get("TERM") != "dumb"
        self.color = self.interactive and "NO_COLOR" not in os.environ
        self.started = time.monotonic()
        self.passed = self.failed = self.pending = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.clear()

    def style(self, text, color):
        return f"\033[{color}m{text}\033[0m" if self.color else text

    def clear(self):
        if self.pending:
            print("\r" + " " * self.pending + "\r", end="", flush=True)
            self.pending = 0

    def write(self, text=""):
        self.clear()
        print(text, flush=True)

    def section(self, number, title):
        if not self.quiet:
            self.write(self.style(f"\n[{number}/6] {title}", 36))

    def note(self, text, verbose=False):
        if not self.quiet and (not verbose or self.verbose):
            self.write(self.style(f"      {text}", 2))

    def result(self, entry: Asset, row: CheckResult):
        failed = row.status == "FAIL"
        self.failed += failed
        self.passed += not failed
        label = f"{urlsplit(row.web_url).netloc}/{row.file}"
        if " | " in row.web_url:
            label = f"{row.file} (reconstructed from all model parts)"
        if (
            failed
            or self.verbose
            or (
                not self.quiet
                and not row.archive_member
                and not entry.prefix
                and entry.check != "gallery"
            )
        ):
            self.write(f"  {self.style(row.status, 31 if failed else 32)}  {label}")
        if failed:
            width = max(40, min(100, shutil.get_terminal_size().columns))
            for name, value in (
                ("Reason", row.detail),
                ("Web", row.web_url),
                ("Source", row.github_source_url),
                ("Release", row.release_asset_url),
                ("Member", row.release_member),
            ):
                if value:
                    self.write(
                        textwrap.fill(
                            f"{name}: {value}",
                            width=width,
                            initial_indent="        ",
                            subsequent_indent="        ",
                        )
                    )
        if self.interactive and not self.quiet and not self.verbose:
            progress = f"      {self.passed} passed, {self.failed} failed"
            print("\r" + progress, end="", flush=True)
            self.pending = len(progress)

    def summary(self, report=None):
        minutes, seconds = divmod(round(time.monotonic() - self.started), 60)
        elapsed = (f"{minutes}m " if minutes else "") + f"{seconds}s"
        status = "FAIL" if self.failed else "PASS"
        self.write(
            f"\n  {self.style(status, 31 if self.failed else 32)}  {self.passed} passed, {self.failed} failed ({elapsed})"
        )
        if report:
            self.write(f"  Report: {report.resolve()}")


class Audit:
    def __init__(
        self, downloads, inventory: Inventory, workers, quiet=False, terminal=None
    ):
        self.downloads = downloads
        self.inventory = inventory
        self.workers = workers
        self.terminal = terminal or Terminal(quiet=quiet)
        self.rows: list[CheckResult] = []
        self.release_cache = {}
        self.release_paths = {}
        self.trees = {}
        self.allowed = {entry.web_url for entry in inventory.files}

    def record(self, entry: Asset, error="", suffix=""):
        row = CheckResult(
            file=entry.file + suffix,
            web_url=entry.web_url,
            github_source_url=entry.github_source_url,
            release_asset_url=entry.release_asset_url,
            archive_member=suffix.removeprefix("!/"),
            release_member=entry.member or suffix.removeprefix("!/"),
            status="FAIL" if error else "PASS",
            detail=error or "matches newest GitHub content",
        )
        self.rows.append(row)
        self.terminal.result(entry, row)

    def run(self, entry, operation):
        try:
            operation(entry)
        except Exception as exc:
            self.record(entry, str(exc) or type(exc).__name__)

    def group(self, entries, operation):
        error = ""
        start = len(self.rows)
        try:
            operation(entries)
        except Exception as exc:
            error = str(exc) or type(exc).__name__
        recorded = {r.web_url for r in self.rows[start:] if not r.archive_member}
        for entry in entries:
            if entry.web_url not in recorded:
                self.record(entry, error or "file was not verified")

    def source_inventory(self):
        """New tracked public/source assets must be added to the inventory."""
        sources = {
            entry.github_source_url
            for entry in (*self.inventory.files, *self.inventory.expand)
        }
        for repo, origin in self.inventory.public_repositories.items():
            commit, tree = self.tree(repo)
            for name in sorted(tree):
                if name in {
                    "public/_headers",
                    "public/_redirects",
                }:
                    continue  # Hosting configuration is not served as an asset.
                if name.startswith("public/"):
                    file = name.removeprefix("public/")
                    listed = origin + "/" + file in self.allowed
                elif name.startswith("src/assets/"):
                    file = name
                    listed = f"https://github.com/{repo}/blob/main/{name}" in sources
                else:
                    continue
                url = origin + "/" + file
                if not listed:
                    self.record(
                        Asset(
                            file=file,
                            web_url=url,
                            github_source_url=f"https://github.com/{repo}/blob/{commit}/{name}",
                        ),
                        "new GitHub asset missing from inventory",
                    )

    def check(self, entry):
        check = entry.check
        if check == "source":
            self.source_file(entry)
        elif check == "release-tag":
            require(self.inventory.hardware_tag, "hardware release was not resolved")
            self.compare(entry, (self.inventory.hardware_tag + "\n").encode())
        elif check in {"member", "html"}:
            self.member(entry)
        elif check in {"release", "repack"}:
            self.compare(
                entry,
                self.release_file(entry),
                zipped=entry.file.endswith(".zip"),
                repack=check == "repack",
            )
        else:
            raise ValueError(f"unknown inventory check: {check}")

    def tree(self, repo, ref="main"):
        key = repo, ref
        if key not in self.trees:
            commit = self.downloads.json(
                f"{API}/repos/{repo}/commits/{quote(ref, safe='')}"
            )["sha"]
            result = self.downloads.json(
                f"{API}/repos/{repo}/git/trees/{commit}?recursive=1"
            )
            require(
                not result.get("truncated"), f"GitHub tree truncated: {repo}@{commit}"
            )
            self.trees[key] = self.trees[repo, commit] = (
                commit,
                {r["path"]: r["sha"] for r in result["tree"] if r["type"] == "blob"},
            )
            self.terminal.note(f"Source: {repo}@{commit[:12]}", verbose=True)
        return self.trees[key]

    def release(self, tag):
        if tag not in self.release_cache:
            self.release_cache[tag] = self.downloads.json(
                f"{API}/repos/{REPO}/releases/tags/{tag}"
            )
        return self.release_cache[tag]

    def newest_hardware(self, pinned):
        if pinned:
            return pinned
        releases = []
        page = 1
        while True:
            batch = self.downloads.json(
                f"{API}/repos/{REPO}/releases?per_page=100&page={page}"
            )
            releases.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        candidates = []
        for release in releases:
            version = release["tag_name"].removeprefix(HARDWARE_PREFIX)
            if (
                release["tag_name"].startswith(HARDWARE_PREFIX)
                and re.fullmatch(r"\d+(\.\d+)*", version)
                and not (release["draft"] or release["prerelease"])
            ):
                candidates.append((tuple(map(int, version.split("."))), release))
        require(candidates, "no published hardware release found")
        width = max(len(version) for version, _ in candidates)
        newest = max(
            candidates, key=lambda item: item[0] + (0,) * (width - len(item[0]))
        )[1]
        self.release_cache[newest["tag_name"]] = newest
        return newest["tag_name"]

    def release_file(self, entry):
        url = entry.release_asset_url
        if url in self.release_paths:
            return self.release_paths[url]
        tag, name = url.removeprefix(RELEASES + "/").split("/", 1)
        matches = [a for a in self.release(tag)["assets"] if a["name"] == name]
        require(len(matches) == 1, f"release {tag} must contain exactly one {name}")
        asset = matches[0]
        path = self.downloads.get(asset["browser_download_url"])
        require(
            path.stat().st_size == asset["size"], f"GitHub asset size mismatch: {url}"
        )
        if asset.get("digest"):
            require(
                "sha256:" + file_sha(path) == asset["digest"],
                f"GitHub asset digest mismatch: {url}",
            )
        self.release_paths[url] = path
        return path

    def compare(self, entry, expected, actual=None, zipped=False, repack=False):
        path = actual or self.downloads.get(entry.web_url)
        data = path.read_bytes()
        prefix = data.lstrip()[:15].lower()
        require(
            data and not prefix.startswith((b"<!doctype html", b"<html")),
            "received HTML instead of a download",
        )
        if zipped:
            want = archive_files(expected)
            have = archive_files(path)
            if repack:
                require(len(want) == 1, "assembly release ZIP must contain one file")
                old = next(iter(want))
                target = PurePosixPath(entry.file).stem + PurePosixPath(old).suffix
                want = {target: want[old]}
                entry = replace(entry, member=old)
            self.compare_members(entry, want, have)
        if not repack:
            expected_hash = (
                sha(expected) if isinstance(expected, bytes) else file_sha(expected)
            )
            actual_hash = sha(data)
            require(
                actual_hash == expected_hash,
                f"stale or different content: served sha256={actual_hash}, newest={expected_hash}",
            )
        self.record(entry)

    def compare_members(self, entry, want, have):
        for name in sorted(want.keys() | have.keys()):
            error = ""
            if name not in have:
                error = "missing file"
            elif name not in want:
                error = "extra file"
            elif want[name] != have[name]:
                error = f"stale or different content: served sha256={have[name]}, newest={want[name]}"
            self.record(entry, error, "!/" + name)
        require(want == have, "ZIP file list/content differs; see member rows")

    def source_file(self, entry):
        match = re.fullmatch(
            r"https://github.com/([^/]+/[^/]+)/blob/([^/]+)/(.+)",
            entry.github_source_url,
        )
        require(match, "invalid GitHub source URL in inventory")
        repo, ref, name = match.groups()
        commit, tree = self.tree(repo, ref)
        entry = replace(
            entry, github_source_url=f"https://github.com/{repo}/blob/{commit}/{name}"
        )
        require(name in tree, f"file missing from newest GitHub source: {name}")
        data = self.downloads.get(entry.web_url).read_bytes()
        require(
            blob(data) == tree[name],
            f"stale or different content from {repo}@{commit}:{name}",
        )
        self.record(entry)

    def html(self, entry, expected):
        text = expected.decode("utf-8")
        if entry.rewrite_downloads:
            names = "|".join(
                re.escape(name) for name in self.inventory.rewrite_downloads
            )
            text = re.sub(
                r"https://github\.com/[^\"'\s]*/releases/(?:download/physiclaw-hardware-v[^\"'\s/]+/|latest/download/)("
                + names
                + r")",
                lambda m: "/downloads/" + m[1],
                text,
            )
        if not re.search(r'<link[^>]*rel=["\']?(?:shortcut )?icon', text, re.I):
            text = re.sub(
                r"<head(\s[^>]*)?>",
                lambda m: m[0] + "\n" + FAVICONS,
                text,
                count=1,
                flags=re.I,
            )
        actual = self.downloads.get(entry.web_url).read_bytes()
        require(
            actual == text.encode(),
            "served HTML differs from newest release after the site's documented rewrites",
        )
        self.record(entry)

    def member(self, entry):
        path = self.release_file(entry)
        with zipfile.ZipFile(path, metadata_encoding="utf-8") as archive:
            expected = archive.read(entry.member)
        if entry.check == "html":
            self.html(entry, expected)
        else:
            self.compare(entry, expected)

    def expanded_files(self, rule):
        with zipfile.ZipFile(
            self.release_file(rule), metadata_encoding="utf-8"
        ) as archive:
            entries = []
            for member in archive.namelist():
                if member.endswith("/") or f"/{rule.prefix}" not in "/" + member:
                    continue
                # fetch-release.mjs copies assets with basename(), flattening dirs.
                name = PurePosixPath(member).name
                entries.append(
                    replace(
                        rule,
                        file=rule.file + name,
                        web_url=rule.web_url + name,
                        member=member,
                        check="member",
                    )
                )
            require(entries, f"no release files under {rule.prefix}")
            require(
                len({entry.web_url for entry in entries}) == len(entries),
                "manual assets have duplicate served filenames after flattening",
            )
            self.allowed.update(entry.web_url for entry in entries)
            # Prefetch independent files; comparisons and output remain ordered.
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                pending = [
                    pool.submit(self.downloads.get, entry.web_url) for entry in entries
                ]
                for entry, future in zip(entries, pending, strict=True):
                    try:
                        path = future.result()
                        self.compare(entry, archive.read(entry.member), actual=path)
                    except Exception as exc:
                        self.record(entry, str(exc) or type(exc).__name__)

    def source_images(self, rule):
        recorded = set()

        def check_page(page):
            text = self.downloads.get(page.web_url).read_text(encoding="utf-8")
            name = PurePosixPath(rule.file)
            pattern = re.escape(name.stem) + r"\.[\w-]+" + re.escape(name.suffix)
            urls = {
                urljoin(page.web_url, ref)
                for ref in References(text).refs
                if re.fullmatch(pattern, PurePosixPath(urlsplit(ref).path).name)
            }
            require(len(urls) == 1, f"expected exactly one built {rule.file} image")
            url = urls.pop()
            require(
                urlsplit(url).netloc == urlsplit(page.web_url).netloc,
                f"built image is not served by the expected website: {url}",
            )
            self.allowed.add(url)
            if url not in recorded:
                self.source_file(
                    replace(
                        rule,
                        file=unquote(urlsplit(url).path.lstrip("/")),
                        web_url=url,
                    )
                )
                recorded.add(url)

        for locale in ("en", "zh"):
            self.run(
                replace(
                    rule,
                    file=f"{locale}/",
                    web_url=rule.web_url.format(locale=locale),
                ),
                check_page,
            )

    def vision(self, entries):
        require(
            [e.part for e in entries] == list(range(len(entries))),
            "vision part list must be contiguous from 00",
        )
        paths = [self.downloads.get(entry.web_url) for entry in entries]
        expected = self.release_file(entries[0])
        encoded = base64.b64encode(expected.read_bytes())
        chunk_size = (len(encoded) + len(entries) - 1) // len(entries)
        for index, path in enumerate(paths):
            require(
                path.read_bytes()
                == encoded[index * chunk_size : (index + 1) * chunk_size],
                f"stale or incorrectly split vision part: {entries[index].web_url}",
            )
        del encoded
        # Parts split the base64 TEXT, potentially in the middle of a quartet.
        output = self.downloads.directory / "vision.zip"
        tail = b""
        with output.open("wb") as stream:
            for path in paths:
                with path.open("rb") as part:
                    while chunk := part.read(256 * 1024):
                        chunk = tail + chunk
                        end = len(chunk) // 4 * 4
                        stream.write(base64.b64decode(chunk[:end], validate=True))
                        tail = chunk[end:]
            require(not tail, "truncated base64 vision model")
        combined = replace(
            entries[0],
            file="downloads/local_vision_model.zip",
            web_url=" | ".join(e.web_url for e in entries),
        )
        self.compare(combined, expected, actual=output, zipped=True)
        for entry in entries:
            self.record(entry)

    def skills(self, entries):
        by_check = {e.check: e for e in entries}
        pack = by_check["skills"]
        path = self.downloads.get(pack.web_url)
        latest = by_check["skills-latest"]
        metadata = self.downloads.json(latest.web_url)
        checksum = by_check["skills-checksum"]
        declared = (
            self.downloads.get(checksum.web_url).read_text(encoding="utf-8").split()
        )
        require(
            len(declared) == 2
            and declared[1].lstrip("*") == PurePosixPath(pack.file).name,
            "invalid skills checksum filename",
        )
        require(
            declared[0].lower() == file_sha(path),
            "skills ZIP/checksum mismatch",
        )
        self.record(checksum)
        with zipfile.ZipFile(path, metadata_encoding="utf-8") as archive:
            all_files = archive_files(path)
            require(
                archive.namelist().count("source.json") == 1,
                "skills ZIP must contain exactly one source.json",
            )
            source = json.loads(archive.read("source.json"))
            commit, tree = self.tree(REPO)
            wanted = {
                name: value
                for name, value in tree.items()
                if name.startswith("skills/")
            }
            require(wanted, "newest GitHub source has no skills")
            current = {
                name: blob(archive.read(name))
                for name in archive.namelist()
                if not name.endswith("/") and name != "source.json"
            }
            # Verify both the newest file set and the pack's declared provenance.
            _, provenance = self.tree(REPO, source["commit"])
            require(
                source.get("repo") == REPO and source.get("subdir") == "skills",
                "incorrect skills provenance",
            )
            require(
                current
                == {n: h for n, h in provenance.items() if n.startswith("skills/")},
                "skills files differ from declared source commit",
            )
            for name in sorted(wanted.keys() | current.keys()):
                error = (
                    "missing file"
                    if name not in current
                    else "extra file"
                    if name not in wanted
                    else "stale or different content"
                    if current[name] != wanted[name]
                    else ""
                )
                child = replace(
                    pack,
                    github_source_url=f"https://github.com/{REPO}/blob/{commit}/{name}",
                )
                self.record(child, error, "!/" + name)
            require(
                current == wanted,
                "skills ZIP differs from newest GitHub skills file list/content",
            )
            names = {
                n.split("/")[1]
                for n in wanted
                if n.count("/") >= 2 and n.endswith("/SKILL.md")
            }
            require(
                {s["name"] for s in source["skills"]} == names
                and len(source["skills"]) == len(names),
                "skills manifest file list differs",
            )
            for skill in source["skills"]:
                prefix = f"skills/{skill['name']}/"
                digest = hashlib.sha256()
                for name in sorted(n for n in current if n.startswith(prefix)):
                    digest.update(
                        name.removeprefix(prefix).encode() + b"\0" + archive.read(name)
                    )
                require(
                    skill.get("path") == prefix.rstrip("/")
                    and skill.get("hash") == "sha256:" + digest.hexdigest(),
                    f"invalid skills manifest hash/path: {skill['name']}",
                )
            for name in sorted(n for n in all_files if "!/" in n):
                self.record(pack, suffix="!/" + name)
        self.record(pack, suffix="!/source.json")
        self.record(pack)
        require(
            metadata.get("schemaVersion") == source.get("schemaVersion") == 1
            and metadata.get("commit") == source["commit"]
            and metadata.get("builtAt") == source.get("builtAt")
            and metadata.get("skillCount") == len(names),
            "latest.json differs from packaged source.json",
        )
        self.record(latest)

    def gallery(self, rule):
        entries = tuple(
            replace(
                rule,
                file=rule.file.format(locale=locale),
                web_url=rule.web_url.format(locale=locale),
            )
            for locale in ("en", "zh")
        )

        def check_pages(entries):
            expected = archive_files(self.release_file(entries[0]))
            recorded = set()
            for entry in entries:
                self.run(
                    entry, lambda page: self.gallery_page(page, expected, recorded)
                )

        self.group(entries, check_pages)

    def gallery_page(self, entry, expected, recorded):
        data = self.downloads.get(entry.web_url).read_text(encoding="utf-8")
        refs = References(data).refs
        urls = sorted(
            {
                urljoin(entry.web_url, ref)
                for ref in refs
                if re.search(r"/gallery_[^/]+\.webp(?:\?|$)", ref)
            }
        )
        require(urls, f"gallery has no images: {entry.web_url}")
        for url in urls:
            require(
                urlsplit(url).netloc == urlsplit(entry.web_url).netloc,
                f"gallery image is not served by the expected website: {url}",
            )
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            paths = list(pool.map(self.downloads.get, urls))
        have = {url: file_sha(path) for url, path in zip(urls, paths, strict=True)}
        covered = set()
        for url, digest in have.items():
            matching = [
                name
                for name, value in expected.items()
                if value == digest
                and re.fullmatch(
                    re.escape(PurePosixPath(name).stem) + r"(?:\.[^.]+)?\.webp",
                    unquote(PurePosixPath(urlsplit(url).path).name),
                )
            ]
            child = replace(
                entry,
                file=unquote(urlsplit(url).path.lstrip("/")),
                web_url=url,
                member=matching[0] if matching else "",
            )
            covered.update(matching)
            if url not in recorded:
                self.record(
                    child,
                    "image filename/content not in newest gallery release"
                    if not matching
                    else "",
                )
                recorded.add(url)
        for name in expected:
            require(
                name in covered,
                f"newest gallery file missing from {entry.web_url}: {name}",
            )
        self.record(entry)

    def linked_inventory(self):
        """Check discovered download/manual links against the explicit file list."""
        for entry in self.inventory.files:
            if entry.check != "html":
                continue
            try:
                text = self.downloads.get(entry.web_url).read_text(encoding="utf-8")
                for ref in sorted(References(text).refs):
                    url = urljoin(entry.web_url, ref).split("#", 1)[0]
                    local = urlsplit(url)
                    root = urlsplit(entry.web_url)
                    if local.netloc == root.netloc and (
                        local.path.startswith("/downloads/") or "/assets/" in local.path
                    ):
                        require(
                            unquote(url) in self.allowed,
                            f"extra linked file outside inventory: {url}",
                        )
            except Exception as exc:
                self.record(entry, str(exc) or type(exc).__name__)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report", type=Path, help="write every file/member row as CSV"
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="print configured inventory without network requests",
    )
    parser.add_argument(
        "--hardware-tag",
        help="check a pinned hardware tag instead of the newest published version",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30,
        help="socket timeout in seconds (default: 30)",
    )
    parser.add_argument("--workers", type=int, default=6)
    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument(
        "--quiet", action="store_true", help="print failures and summary only"
    )
    verbosity.add_argument(
        "--verbose", action="store_true", help="show every file and ZIP member"
    )
    args = parser.parse_args(argv)
    require(
        args.timeout > 0 and args.workers > 0, "timeout and workers must be positive"
    )
    inventory = INVENTORY
    if args.list:
        print(json.dumps(asdict(inventory), ensure_ascii=False, indent=2))
        return 0
    with (
        Terminal(quiet=args.quiet, verbose=args.verbose) as terminal,
        tempfile.TemporaryDirectory(prefix="physiclaw-assets-") as directory,
    ):
        if not args.quiet:
            terminal.write(terminal.style("\nPhysiClaw asset check", 1))
            terminal.note(
                "physiclaw.ai + docs.physiclaw.ai -> current GitHub sources/releases"
            )
        audit = Audit(
            Downloads(Path(directory), args.timeout),
            inventory,
            args.workers,
            terminal=terminal,
        )
        terminal.section(1, "Find the newest hardware release")
        tag = audit.newest_hardware(args.hardware_tag)
        terminal.note(
            tag + (" (pinned)" if args.hardware_tag else " (newest published version)")
        )
        # Only the hardware tag is templated here; gallery locale placeholders
        # belong to the gallery expansion and must survive this substitution.
        inventory = inventory.for_hardware(tag)
        audit.inventory = inventory
        terminal.section(2, "Check the listed downloads and source files")
        seen = set()
        for entry in inventory.files:
            require(
                entry.web_url not in seen,
                f"duplicate inventory URL: {entry.web_url}",
            )
            require(
                unquote(urlsplit(entry.web_url).path).lstrip("/") == entry.file,
                f"inventory filename/URL mismatch: {entry.file}",
            )
            seen.add(entry.web_url)
            check = entry.check
            if check in {"vision", "skills", "skills-checksum", "skills-latest"}:
                continue
            audit.run(entry, audit.check)
        vision = [e for e in inventory.files if e.check == "vision"]
        terminal.section(
            3, f"Reconstruct and verify the vision model ({len(vision)} parts)"
        )
        audit.group(vision, audit.vision)
        skills = [e for e in inventory.files if e.check.startswith("skills")]
        terminal.section(4, "Verify official skills, checksum and metadata")
        audit.group(skills, audit.skills)
        terminal.section(5, "Check manual diagrams and site images")
        for rule in inventory.expand:
            operation = {
                "members": audit.expanded_files,
                "source-image": audit.source_images,
                "gallery": audit.gallery,
            }[rule.check]
            audit.run(rule, operation)
        terminal.section(6, "Check for missing or unlisted assets")
        audit.linked_inventory()
        audit.source_inventory()
        if args.report:
            with args.report.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(
                    stream,
                    fieldnames=[field.name for field in fields(CheckResult)],
                )
                writer.writeheader()
                writer.writerows(asdict(row) for row in audit.rows)
        terminal.summary(args.report)
        return int(terminal.failed > 0)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError) as exc:
        print(f"\n  ERROR  Asset check could not finish: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n  Asset check interrupted.", file=sys.stderr)
        sys.exit(130)
