"""Download and install the official skill pack.

The remote commit identifies the release; the downloaded ZIP's SHA-256
checks transport integrity. Per-skill hashes diagnose manifest drift only.
Syncs are serialized across processes. Installation uses same-filesystem
renames and restores the previous tree and metadata on failure. Readers
can briefly see a missing path during a swap; existing session snapshots
remain unchanged.
"""

import datetime as dt
import hashlib
import http.client
import json
import logging
import os
import shutil
import tempfile
import threading
import urllib.error
import zipfile
from contextlib import suppress
from pathlib import Path
from typing import Callable

import click
import typer

from physiclaw.cli._format import info, ok, warn
from physiclaw.cli._http import http_get, stream
from physiclaw.common import paths
from physiclaw.common.config import load as _load_config
from physiclaw.common.text import read_text, write_text
from physiclaw.port.locks import FileLockBusy, exclusive_file_lock

log = logging.getLogger(__name__)

SYNC_STATE_FILE = ".sync-state.json"
STAGING_DIR = ".sync-staging"
ZIP_NAME = "physiclaw_official_skills.zip"
SCHEMA_VERSION = 1


class _NotPublished(Exception):
    """A metadata URL returned 404; only a missing latest.json is a no-op."""


class SyncError(RuntimeError):
    """A sync failure with a human-readable message. Raised by the helpers
    so each front end picks its own surface: the CLI command converts to
    ``exit_error`` (stderr + exit 1); the server's background auto-sync
    logs it — a daemon thread's stderr is invisible, the daily log isn't."""


def _require_https(url: str) -> None:
    if not url.startswith("https://"):
        raise SyncError(f"refusing non-HTTPS official-skills URL: {url}")


def _urls(base: str) -> dict[str, str]:
    base = base.rstrip("/")
    return {
        "latest": f"{base}/latest.json",
        "zip": f"{base}/{ZIP_NAME}",
        "sha256": f"{base}/{ZIP_NAME}.sha256",
    }


def _fetch_bytes(url: str) -> bytes:
    """Read metadata; distinguish unpublished files from transport errors."""
    _require_https(url)
    try:
        with http_get(url) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        with suppress(OSError):
            e.close()
        if e.code == 404:
            raise _NotPublished from e
        raise SyncError(f"GET {url} failed: HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise SyncError(f"GET {url} failed: {e.reason}") from e


def _fetch_json(url: str) -> dict:
    try:
        data = json.loads(_fetch_bytes(url).decode("utf-8"))
    except ValueError as e:
        raise SyncError(f"{url} did not return valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise SyncError(f"{url} did not return a JSON object.")
    return data


def _read_json_object(path: Path) -> dict:
    """Read optional local metadata; missing or corrupt data is an empty object."""
    try:
        data = json.loads(read_text(path))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _parse_sha256(text: str) -> str | None:
    """``<hex>  <filename>\\n`` → the lowercased 64-char hex, or None if the
    line isn't a valid sha256sum record."""
    parts = text.split()
    if not parts:
        return None
    hexd = parts[0].strip().lower()
    if len(hexd) == 64 and all(c in "0123456789abcdef" for c in hexd):
        return hexd
    return None


def _download_to_temp(url: str) -> tuple[Path, str]:
    """Stream ``url`` (HTTPS only) to a temp file, hashing as we go so we never
    hold the whole pack in memory. Returns ``(temp_path, sha256_hex)``; the
    caller is responsible for unlinking the path. The temp file lives in the
    system temp dir (``/tmp`` on Linux) — only the later staging→install
    ``os.replace`` needs to be same-filesystem, not this download."""
    _require_https(url)
    digest = hashlib.sha256()
    fd, name = tempfile.mkstemp(prefix="physiclaw-official-", suffix=".zip")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as f, http_get(url) as resp:
            # Reuse the shared chunked reader (64 KiB chunks), hashing each
            # chunk on the way to disk — constant memory. progress=False: the
            # skills pack is small, so no progress bar — just the synced result.
            def _write(chunk: bytes) -> None:
                f.write(chunk)
                digest.update(chunk)

            stream(resp, _write, "  official skills", progress=False)
    except BaseException as e:
        with suppress(OSError):
            tmp.unlink(missing_ok=True)
        if isinstance(e, urllib.error.HTTPError):
            with suppress(OSError):
                e.close()
            raise SyncError(f"GET {url} failed: HTTP {e.code}") from e
        if isinstance(e, urllib.error.URLError):
            raise SyncError(f"GET {url} failed: {e.reason}") from e
        raise
    return tmp, digest.hexdigest()


def _extract_zip(zip_path: Path, dest: Path) -> None:
    """Extract the zip file into ``dest`` with zip-slip protection: every
    member's resolved path must stay within ``dest`` or the whole extract is
    refused (a malicious ``../`` entry can't escape staging)."""
    dest_real = dest.resolve()
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for member in zf.namelist():
                target = (dest / member).resolve()
                if target != dest_real and not target.is_relative_to(dest_real):
                    raise SyncError(
                        f"zip entry escapes staging dir: {member!r} — "
                        "aborting (possible zip-slip)."
                    )
            zf.extractall(dest)
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError) as e:
        raise SyncError(f"downloaded pack is not a valid zip: {e}") from e


def _sanity_check_layout(staging: Path) -> None:
    """The extracted top level must be exactly ``source.json`` + ``skills/``.
    Anything else (stray files, ``__MACOSX``, a missing half) means a
    malformed pack — refuse rather than mount it."""
    entries = {p.name for p in staging.iterdir()}
    if (
        entries != {"source.json", "skills"}
        or not (staging / "skills").is_dir()
        or not (staging / "source.json").is_file()
    ):
        raise SyncError(
            f"unexpected package layout {sorted(entries)} — expected "
            "exactly source.json + skills/."
        )


def _count_skills(skills_dir: Path) -> int:
    """Count visible skill directories in a validated pack."""
    return sum(
        1 for d in skills_dir.iterdir() if d.is_dir() and not d.name.startswith(".")
    )


def _hash_skill(skill_dir: Path) -> str:
    """Content digest of one skill dir. Mirrors the site builder's ``hashSkill``
    (PhysiClaw-site ``scripts/build-official-skills.mjs``) byte-for-byte so the
    digests line up: sha256 over each file's ``/``-joined path RELATIVE to the
    skill dir, then a NUL, then the file bytes — files sorted by that relpath.
    Prefixed ``sha256:`` so the algorithm can migrate later."""
    files = sorted(
        p.relative_to(skill_dir).as_posix() for p in skill_dir.rglob("*") if p.is_file()
    )
    h = hashlib.sha256()
    for rel in files:
        h.update(rel.encode("utf-8"))
        h.update(b"\x00")
        h.update((skill_dir / rel).read_bytes())
    return f"sha256:{h.hexdigest()}"


def _verify_skill_hashes(skills_root: Path, manifest_skills: object) -> list[str]:
    """Warn about malformed entries and drift; never follow a manifest path out."""
    from physiclaw.agent.engine.skill import is_safe_skill_name

    if not isinstance(manifest_skills, list):
        return ["manifest skills must be a list"]
    notes: list[str] = []
    for entry in manifest_skills:
        if not isinstance(entry, dict):
            notes.append("manifest skill entry must be an object")
            continue
        name, declared = entry.get("name"), entry.get("hash")
        if not isinstance(name, str) or not is_safe_skill_name(name):
            notes.append(f"invalid manifest skill name: {name!r}")
            continue
        if declared is None:
            continue  # Older manifests may omit per-skill hashes.
        if not isinstance(declared, str) or not declared:
            notes.append(f"{name}: invalid manifest hash")
            continue
        d = skills_root / name
        if not d.is_dir():
            notes.append(f"{name}: in manifest but not extracted")
            continue
        actual = _hash_skill(d)
        if actual != declared:
            notes.append(f"{name}: {actual[:19]}… ≠ manifest {declared[:19]}…")
    return notes


def _write_sync_state(
    staging: Path, url: str, zip_hex: str, commit: str, count: int
) -> None:
    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "url": url,
        "zipHash": f"sha256:{zip_hex}",
        "commit": commit,
        "skillCount": count,
        "syncedAt": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    write_text(staging / SYNC_STATE_FILE, json.dumps(payload, indent=2) + "\n")


def _install(staging: Path, official: Path) -> None:
    """Replace owned paths; keep backups until every replacement succeeds."""
    backup = Path(tempfile.mkdtemp(prefix=".sync-backup-", dir=official))
    parked: list[str] = []
    installed: list[str] = []
    names = ("skills", "source.json", SYNC_STATE_FILE)
    try:
        for name in names:
            live = official / name
            if live.exists() or live.is_symlink():
                os.replace(live, backup / name)
                parked.append(name)
            os.replace(staging / name, live)
            installed.append(name)
    except BaseException as error:
        failures: list[str] = []
        for name in reversed(names):
            try:
                if name in installed:
                    os.replace(official / name, staging / name)
                if name in parked:
                    os.replace(backup / name, official / name)
            except OSError as e:
                failures.append(f"{name}: {e}")
        if failures:
            detail = f"restore failed ({'; '.join(failures)}); backups kept at {backup}"
            if isinstance(error, Exception):
                raise SyncError(f"{error}; {detail}") from error
            log.error("%s", detail)
        else:
            shutil.rmtree(backup, ignore_errors=True)
        raise
    else:
        shutil.rmtree(backup, ignore_errors=True)
        # Reclaim the old implementation's scratch directories only after success.
        shutil.rmtree(official / ".sync-trash", ignore_errors=True)
        shutil.rmtree(official / STAGING_DIR, ignore_errors=True)


def sync(
    *,
    force: bool = False,
    dry_run: bool = False,
    emit: Callable[[str], None] = typer.echo,
) -> None:
    """Install a changed release; dry-run checks freshness without writing.

    Expected input, filesystem, and transport failures raise SyncError for the
    CLI or background logger. Cancellation and operator interrupts propagate.
    """
    try:
        if dry_run:
            _sync(force=force, dry_run=True, emit=emit)
        else:
            with exclusive_file_lock(paths.official_dir() / ".sync.lock"):
                _sync(force=force, dry_run=False, emit=emit)
    except FileLockBusy as e:
        raise SyncError("official skills sync is already running") from e
    except (OSError, ValueError, http.client.HTTPException) as e:
        raise SyncError(f"official skills sync failed: {e}") from e


def _sync(*, force: bool, dry_run: bool, emit: Callable[[str], None]) -> None:
    urls = _urls(_load_config().skills.official_base_url)
    try:
        latest = _fetch_json(urls["latest"])
    except _NotPublished:
        emit(info("no official skills published yet — nothing to sync."))
        return
    remote_commit = latest.get("commit")
    if not isinstance(remote_commit, str) or not remote_commit.strip():
        raise SyncError(
            f"{urls['latest']} is missing 'commit' or it is not a non-empty string."
        )

    official = paths.official_dir()
    local_commit = _read_json_object(official / SYNC_STATE_FILE).get("commit")
    up_to_date = (
        local_commit == remote_commit
        and (official / "skills").is_dir()
        and (official / "source.json").is_file()
    )
    if dry_run:
        if up_to_date:
            tail = "; --force would re-sync." if force else " — no sync needed."
            emit(info(f"up to date at {remote_commit[:7]}{tail}"))
        elif isinstance(local_commit, str) and local_commit:
            emit(
                info(
                    f"sync would run: update {local_commit[:7]} → {remote_commit[:7]}."
                )
            )
        else:
            emit(info(f"sync would run: install {remote_commit[:7]} (none installed)."))
        return
    if up_to_date and not force:
        emit(ok(f"official skills already up to date ({remote_commit[:7]})."))
        return

    zip_path, actual = _download_to_temp(urls["zip"])
    try:
        try:
            sha_body = _fetch_bytes(urls["sha256"])
        except _NotPublished as e:
            raise SyncError(
                f"{urls['sha256']} is not published yet — the pack may be "
                "mid-publish; retry shortly."
            ) from e
        expected = _parse_sha256(sha_body.decode("utf-8", "replace"))
        if expected is None:
            raise SyncError(f"{urls['sha256']} is not a valid sha256sum line.")
        if actual != expected:
            raise SyncError(
                f"checksum mismatch — expected {expected[:12]}…, got "
                f"{actual[:12]}…. Aborting; the install was not touched."
            )
        staging = Path(tempfile.mkdtemp(prefix=STAGING_DIR + "-", dir=official))
        try:
            _extract_zip(zip_path, staging)
            _sanity_check_layout(staging)
            src = _read_json_object(staging / "source.json")
            declared_commit = src.get("commit")
            if declared_commit and declared_commit != remote_commit:
                raise SyncError(
                    "pack commit does not match latest.json; retry after publishing finishes"
                )
            hash_notes = _verify_skill_hashes(staging / "skills", src.get("skills", []))
            landed = _count_skills(staging / "skills")
            _write_sync_state(staging, urls["zip"], actual, remote_commit, landed)
            _install(staging, official)
        finally:
            # TemporaryDirectory retries permission failures recursively on some
            # Python/platform combinations. A failed cleanup must stay nonfatal.
            shutil.rmtree(staging, ignore_errors=True)
    finally:
        with suppress(OSError):
            zip_path.unlink(missing_ok=True)

    emit(ok(f"synced {landed} official skill(s) @ {remote_commit[:7]}."))
    for note in hash_notes:
        emit(warn(f"skill hash mismatch — {note}"))
    declared = len(src["skills"]) if isinstance(src.get("skills"), list) else 0
    if declared and declared != landed:
        emit(warn(f"manifest lists {declared} skill(s) but {landed} landed on disk."))


def maybe_auto_sync() -> None:
    """Start an optional daemon sync when enabled, outside CI, and alone.

    Read guards synchronously, before this server records itself as live.
    The install lock also excludes an overlapping manual sync.
    """
    from physiclaw.common import runtime_state

    try:
        if not _load_config().skills.sync_auto:
            return
        if os.environ.get("CI", "").strip().lower() in ("1", "true", "yes"):
            return
        if runtime_state.read_live():
            return
        threading.Thread(
            target=_run_sync_quiet, name="physiclaw-auto-sync", daemon=True
        ).start()
    except (OSError, ValueError, RuntimeError) as e:
        log.debug("auto-sync: could not start (non-fatal): %s", e)


def _run_sync_quiet() -> None:
    """The background-thread body: run ``sync`` fully fail-soft — failures
    land in the daily log at DEBUG (file logging always records DEBUG),
    silent on console: the sync is a best-effort nicety and a flaky network
    shouldn't put a warning in every startup. Either way the thread dies
    quietly and the server is never affected."""
    try:
        sync(emit=_log_emit)
    except SyncError as e:
        log.debug("auto-sync: official skills sync failed (non-fatal): %s", e)
    except Exception:
        log.debug("auto-sync: official skills sync failed (non-fatal)", exc_info=True)


def _log_emit(msg: str) -> None:
    """Outcome printer for the background sync: unstyle (no ANSI codes in
    the daily log file) and route through the server's logger so the line
    gets the ``HH:MM [physiclaw]`` shape like every other startup message."""
    log.debug("%s", click.unstyle(msg))


__all__ = ["SyncError", "sync", "maybe_auto_sync"]
