#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["esptool>=4,<5", "littlefs-python>=0.12"]
# ///
"""Build the ``physiclaw flash`` firmware bundle from a FluidNC release.

Combines upstream release assets with the adjacent ``config.yaml`` into
``firmware/output/fluidnc.zip`` for the MKS DLC32 V2.1 board.

    make firmware
    make firmware ARGS="--tag v4.1.0"
    uv run firmware/firmware.py --help

See ``firmware/README.md`` for inputs, bundle contents, validation, and
the publishing workflow. This PEP 723 script runs independently of the
PhysiClaw Python package; uv installs its declared build dependencies.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import time
import urllib.request
import zipfile
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote

DEFAULT_TAG = "v4.1.0"
BUNDLE_NAME = "fluidnc.zip"  # version-free; fluidnc_version.txt inside records it
FIRMWARE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = FIRMWARE_DIR / "output"
CACHE_DIR = OUTPUT_DIR / ".cache"
CONFIG_YAML = FIRMWARE_DIR / "config.yaml"
DEFAULT_OUTPUT = OUTPUT_DIR / BUNDLE_NAME

# Paths inside the upstream POSIX release zip, relative to fluidnc-<tag>-posix/.
BOOT_IMAGES = {
    "bootloader.bin": "wifi/bootloader.bin",
    "partitions.bin": "wifi/partitions.bin",
    "boot_app0.bin": "common/boot_app0.bin",
}

# Mirrors FLASH_LAYOUT in physiclaw.cli.flash (littlefs.bin offset) —
# pinned to it by tests/firmware/test_consistency.py.
LITTLEFS_OFFSET = 0x3D0000
LFS_BLOCK_SIZE = 4096

ORDER = [
    *BOOT_IMAGES,
    "firmware.bin",
    "littlefs.bin",
    "config.yaml",
    "fluidnc_version.txt",
]
# Informational entries — everything else in ORDER is flashed by the CLI.
REFERENCE_FILES = {"config.yaml", "fluidnc_version.txt"}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def styled(message: str, color: str, *, error: bool = False) -> str:
    """Use color in terminals, while keeping redirected logs and NO_COLOR plain."""
    stream = sys.stderr if error else sys.stdout
    if stream.isatty() and "NO_COLOR" not in os.environ and os.getenv("TERM") != "dumb":
        codes = {
            "cyan": "1;36",
            "green": "1;32",
            "yellow": "33",
            "red": "1;31",
            "dim": "2",
        }
        return f"\033[{codes[color]}m{message}\033[0m"
    return message


def log(message: str, color: str = "cyan", *, error: bool = False) -> None:
    stream = sys.stderr if error else sys.stdout
    print(styled(message, color, error=error), file=stream, flush=True)


def fail(message: str) -> None:
    sys.exit(styled(f"error: {message}", "red", error=True))


def fetch(
    url: str, dest: Path, *, digest: str | None = None, size: int | None = None
) -> bool:
    """Reuse only checksum-verified files; resume downloads only within this call."""
    if digest and not dest.is_symlink() and dest.is_file():
        if (size is None or dest.stat().st_size == size) and file_sha256(
            dest
        ) == digest:
            return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Never resume an unverified prefix from a previous run.
    dest.unlink(missing_ok=True)
    if size is not None:
        log(f"↓ {dest.name} · {size / 1048576:.1f} MiB", "dim")
    for attempt in range(1, 9):
        try:
            _download_once(url, dest)
            if (size is not None and dest.stat().st_size != size) or (
                digest is not None and file_sha256(dest) != digest
            ):
                dest.unlink()
                raise ValueError(
                    "download does not match the GitHub asset checksum or size"
                )
            return False
        except Exception as e:  # noqa: BLE001 — any transport error: resume
            if isinstance(e, HTTPError):
                e.close()
                if e.code == 404:
                    fail(
                        "FluidNC release or asset not found (HTTP 404).\n"
                        f"  {url}\n"
                        "Check --tag and available assets at "
                        "https://github.com/bdring/FluidNC/releases."
                    )
                if e.code == 416:
                    # A rejected range does not prove our cached bytes are complete.
                    dest.unlink(missing_ok=True)
            log(f"  Download attempt {attempt}/8 failed: {e}", "yellow", error=True)
            if attempt < 8:
                time.sleep(2)
    fail(f"download failed after 8 attempts: {url}")


def _download_once(url: str, dest: Path) -> None:
    """Append one validated response; leave incomplete bytes for the next attempt."""
    offset = dest.stat().st_size if dest.is_file() else 0
    request = urllib.request.Request(url)
    if offset:
        request.add_header("Range", f"bytes={offset}-")
    with urllib.request.urlopen(request, timeout=60) as response:
        if response.status == 200:  # Server ignored Range: replace the partial file.
            offset = 0
        expected, total = _response_size(response, offset)
        received = 0
        with dest.open("ab" if offset else "wb") as target:
            while chunk := response.read(1 << 16):
                target.write(chunk)
                received += len(chunk)
        if not received or (expected is not None and received != expected):
            raise ValueError("incomplete download response")
        if total is not None and offset + received != total:
            raise ValueError("download has more bytes to fetch")


def _response_size(response, offset: int) -> tuple[int | None, int | None]:
    """Validate a resumed response before appending; return body and file sizes."""
    length = response.headers.get("Content-Length")
    expected = int(length) if length is not None else None
    if expected is not None and expected <= 0:
        raise ValueError("empty or invalid download length")
    if response.status == 200:
        return expected, expected
    if response.status != 206:
        raise ValueError(f"unexpected download status {response.status}")
    match = re.fullmatch(
        r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", "")
    )
    if match is None:
        raise ValueError("missing or invalid Content-Range")
    start, end, total = map(int, match.groups())
    if start != offset or not start <= end < total:
        raise ValueError("Content-Range does not match the cached prefix")
    size = end - start + 1
    if expected is not None and expected != size:
        raise ValueError("Content-Length does not match Content-Range")
    return size, total


def elf2image(elf: Path, out: Path) -> None:
    """Convert the noradio ELF to the ESP32 image expected by the flash command."""
    import esptool  # type: ignore[import-not-found]  # PEP 723 script dep

    args = [
        "--chip", "esp32",
        "elf2image",
        "--flash_mode", "dio",
        "--flash_freq", "80m",
        "--flash_size", "4MB",
        "-o", str(out),
        str(elf),
    ]  # fmt: skip
    diagnostics = io.StringIO()
    try:
        with redirect_stdout(diagnostics):
            esptool.main(args)
    except BaseException as error:
        if isinstance(error, SystemExit) and not error.code:
            return
        if details := diagnostics.getvalue().strip():
            log(details, "red", error=True)
        log(f"Image conversion failed: {error}", "red", error=True)
        raise


def data_partition(partitions: bytes) -> tuple[int, int]:
    """(offset, size) of the spiffs/littlefs data partition in the table."""
    entry_size = 32
    for i in range(0, len(partitions) - entry_size + 1, entry_size):
        entry = partitions[i : i + entry_size]
        if entry[:2] != b"\xaaP":  # end of table
            break
        ptype, subtype = entry[2], entry[3]
        offset, size = struct.unpack_from("<II", entry, 4)
        if ptype == 1 and subtype == 0x82:  # data / spiffs
            return offset, size
    fail("no spiffs/littlefs data partition found in partitions.bin")


def build_littlefs(config: bytes, block_count: int, out: Path) -> None:
    """Build the board's filesystem with /config.yaml as its only file."""
    from littlefs import LittleFS  # type: ignore[import-not-found]  # PEP 723 dep

    fs = LittleFS(block_size=LFS_BLOCK_SIZE, block_count=block_count)
    with fs.open("/config.yaml", "wb") as f:
        f.write(config)
    fs.unmount()
    out.write_bytes(bytes(fs.context.buffer))


def write_bundle(files: dict[str, bytes], out: Path) -> None:
    """Publish a complete deterministic zip, preserving any previous bundle on failure."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".physiclaw-fw-", dir=out.parent) as td:
        pending = Path(td) / out.name
        with zipfile.ZipFile(pending, "w") as zf:
            for name in ORDER:
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3  # fixed Unix attributes on every build host
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                zf.writestr(info, files[name])
        pending.replace(out)


def resolve_release_assets(
    tag: str, *, posix_zip: Path | None = None, elf: Path | None = None
) -> tuple[Path, Path]:
    """Use supplied local assets, downloading only the inputs that were omitted."""
    inputs = {f"fluidnc-{tag}-posix.zip": posix_zip, "esp32-noradio-firmware.elf": elf}
    assets = {}
    if None in inputs.values():
        metadata = CACHE_DIR / tag / "release.json"
        if metadata.parent.is_symlink():
            fail(f"Cache release directory must not be a symlink: {metadata.parent}")
        fetch(
            f"https://api.github.com/repos/bdring/FluidNC/releases/tags/{quote(tag, safe='')}",
            metadata,
        )
        assets = {
            asset["name"]: asset
            for asset in json.loads(metadata.read_bytes())["assets"]
        }
    for name, local in inputs.items():
        label = "POSIX" if name.endswith(".zip") else "ELF"
        if local is not None:
            log(f"• {label} · local input", "dim")
            continue
        if name not in assets:
            fail(
                f"FluidNC {tag} has no {name}.\nCheck --tag and available assets at https://github.com/bdring/FluidNC/releases."
            )
        asset = assets[name]
        digest = asset.get("digest") or ""
        digest = (
            digest.removeprefix("sha256:") if digest.startswith("sha256:") else None
        )
        if digest is not None and not re.fullmatch(r"[0-9a-f]{64}", digest):
            fail(f"Invalid GitHub SHA-256 checksum for {name}")
        dest = CACHE_DIR / tag / name
        cached = fetch(
            asset["browser_download_url"], dest, digest=digest, size=asset["size"]
        )
        if digest is None:
            result = "downloaded fresh; upstream has no SHA-256"
        else:
            result = f"{'cached' if cached else 'downloaded'} · GitHub SHA-256 verified"
        log(f"✓ {label} · {dest.stat().st_size / 1048576:.1f} MiB · {result}", "green")
        inputs[name] = dest
    return inputs[f"fluidnc-{tag}-posix.zip"], inputs["esp32-noradio-firmware.elf"]


def build_bundle(*, tag: str, posix_zip: Path, elf: Path, out: Path) -> None:
    """Build and report a complete flash bundle from local assets; never download."""
    prefix = f"fluidnc-{tag}-posix"
    with zipfile.ZipFile(posix_zip) as archive:
        files = {
            name: archive.read(f"{prefix}/{member}")
            for name, member in BOOT_IMAGES.items()
        }

    offset, size = data_partition(files["partitions.bin"])
    if offset != LITTLEFS_OFFSET:
        fail(
            f"data partition moved to {offset:#x} in {tag} — "
            f"physiclaw.cli.flash writes littlefs.bin at "
            f"{LITTLEFS_OFFSET:#x}; update FLASH_LAYOUT first"
        )
    if size <= 0 or size % LFS_BLOCK_SIZE:
        fail(f"Invalid LittleFS partition size: {size} bytes")
    block_count = size // LFS_BLOCK_SIZE
    config = CONFIG_YAML.read_bytes()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".build-", dir=OUTPUT_DIR) as td:
        work = Path(td)
        elf2image(elf, work / "firmware.bin")
        build_littlefs(config, block_count, work / "littlefs.bin")
        files["firmware.bin"] = (work / "firmware.bin").read_bytes()
        files["littlefs.bin"] = (work / "littlefs.bin").read_bytes()

    files["config.yaml"] = config
    files["fluidnc_version.txt"] = f"{tag}\n".encode()
    write_bundle(files, out)
    cwd = Path.cwd()
    display = out.relative_to(cwd) if out.is_relative_to(cwd) else out
    log(f"✓ {display} · {out.stat().st_size:,} bytes · {len(ORDER)} files", "green")
    log(f"SHA-256 {file_sha256(out)}", "dim")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI options without downloading or importing image build tools."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--tag", default=DEFAULT_TAG, help="FluidNC release tag (default: %(default)s)"
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"output zip (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument("--posix-zip", type=Path, help="local fluidnc posix zip")
    parser.add_argument("--elf", type=Path, help="local esp32-noradio-firmware.elf")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.tag):
        parser.error(
            f"--tag must be a release name such as {DEFAULT_TAG} (no path separators)"
        )
    return args


def _clean_output_dir() -> None:
    """Remove generated outputs, keeping the download cache and its active lock."""
    try:
        for path in OUTPUT_DIR.iterdir():
            if path == CACHE_DIR:
                continue
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
    except OSError as error:
        fail(f"Could not clear {OUTPUT_DIR}: {error}")


@contextmanager
def build_workspace():
    """One builder owns the output tree; OS locks release automatically on exit."""
    for directory in (OUTPUT_DIR, CACHE_DIR):
        if directory.is_symlink():
            directory.unlink()
        directory.mkdir(parents=True, exist_ok=True)
    lock_path = CACHE_DIR / ".build.lock"
    if lock_path.is_symlink():
        fail(f"Remove the unexpected lock symlink: {lock_path}")
    with lock_path.open("a+b") as lock:
        try:
            if os.name == "nt":
                import msvcrt

                if lock.tell() == 0:
                    lock.write(b"\0")
                    lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fail(
                f"Another firmware build is using {OUTPUT_DIR}; wait for it to finish."
            )
        # Keep this file: unlinking it lets another process lock a different inode.
        _clean_output_dir()
        yield


def main(argv: list[str] | None = None) -> None:
    """Clear managed outputs, resolve release inputs, and build the bundle."""
    args = parse_args(argv)
    resolved_out = args.out.resolve()
    if (
        Path(os.path.abspath(args.out)).is_relative_to(CACHE_DIR)
        or resolved_out.is_relative_to(CACHE_DIR.resolve())
        or resolved_out == OUTPUT_DIR.resolve()
    ):
        fail("--out must be a bundle file outside output/.cache")
    for source in (args.posix_zip, args.elf, CONFIG_YAML, Path(__file__)):
        if source is None:
            continue
        resolved_source = source.resolve()
        if resolved_source == resolved_out:
            fail(f"Output would overwrite an input: {source}")
        if any(
            path.is_relative_to(OUTPUT_DIR) and not path.is_relative_to(CACHE_DIR)
            for path in (Path(os.path.abspath(source)), resolved_source)
        ):
            fail(
                f"Local input would be deleted during cleanup: {source}. Move it outside output/ or into output/.cache/."
            )
    try:
        with build_workspace():
            log(f"FluidNC {args.tag} · ESP32 noradio · clean output · cache kept")
            posix_zip, elf = resolve_release_assets(
                args.tag, posix_zip=args.posix_zip, elf=args.elf
            )
            build_bundle(tag=args.tag, posix_zip=posix_zip, elf=elf, out=args.out)
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        fail(str(error))
    except KeyboardInterrupt:
        fail("Build cancelled.")


if __name__ == "__main__":
    main()
