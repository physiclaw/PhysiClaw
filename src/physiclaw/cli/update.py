"""Optional update notices and cache warming for server startup.

``physiclaw update`` prints the manual upgrade command. Startup notification
reads a marker left by a previous background check; staging probes PyPI,
warms uv's cache in a separate environment, and records the newer version.
Neither phase reinstalls the running tool, which could corrupt a live
installation on Windows while Python and native libraries are locked.

Config ``update.check = false``, ``PHYSICLAW_DISABLE_UPDATE_CHECK=1``, and CI
skip both phases. Filesystem, subprocess, and worker-start failures must not
prevent the server from starting.
"""

import json
import logging
import os
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

import typer

from physiclaw import __version__ as _pkg_version
from physiclaw.cli._update_check import (
    _disabled_via_env,
    _fetch_pypi_version,
    _is_newer,
    _write_cache,
)
from physiclaw.common import config, paths
from physiclaw.common.text import read_text, write_text

log = logging.getLogger(__name__)

# Bound the background cache warm and the synchronous installed-tool query.
_UV_WARM_TIMEOUT = 600
_UV_QUERY_TIMEOUT = 30


def _update_check_disabled() -> bool:
    """Skip optional update work when disabled by config, environment, or CI."""
    return (
        _disabled_via_env()
        or os.environ.get("CI", "").strip().lower() in ("1", "true", "yes")
        or not config.CONFIG.update.check
    )


def _run(
    cmd: list[str], *, timeout: int = _UV_WARM_TIMEOUT
) -> subprocess.CompletedProcess | None:
    """Run a uv subcommand, capturing UTF-8 output; None if it couldn't run at
    all (missing exe, timeout). Explicit UTF-8 because uv emits UTF-8 regardless
    of the console codepage, and non-UTF-8 Windows locales (cp936, cp1252) would
    make locale-default decoding throw (same case config.py handles for
    config.toml)."""
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _has_tool_install(uv: str) -> bool:
    """Look for ``physiclaw v...`` in ``uv tool list``.

    Entry-point rows start with ``-`` and do not identify an installed tool.
    """
    out = _run([uv, "tool", "list"], timeout=_UV_QUERY_TIMEOUT)
    if out is None or out.returncode != 0:
        return False
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "physiclaw" and parts[1].startswith("v"):
            return True
    return False


def update() -> None:
    """Show how to update physiclaw (it's a uv tool — update it with uv)."""
    typer.echo("To update, stop the server and run:\n\n    uv tool upgrade physiclaw")


def _stage_file() -> Path:
    return paths.cache_dir() / "update.json"


def _read_staged() -> str | None:
    """The newer version a prior background stage flagged as ready, or None
    (missing / unreadable / malformed marker)."""
    p = _stage_file()
    try:
        data = json.loads(read_text(p))
    except (OSError, ValueError):
        return None
    v = data.get("version") if isinstance(data, dict) else None
    return v if isinstance(v, str) and v else None


def _write_staged(version: str) -> bool:
    try:
        p = _stage_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        write_text(
            p,
            json.dumps(
                {
                    "version": version,
                    "staged_at": datetime.now(timezone.utc).isoformat(),
                }
            ),
        )
    except OSError:
        return False  # Phase B retries next cycle.
    return True


def _clear_staged() -> None:
    try:
        _stage_file().unlink(missing_ok=True)
    except OSError:
        pass


def _warm_cmd(uv: str, version: str) -> list[str]:
    """Warm uv's global cache for ``physiclaw==version`` WITHOUT touching the
    installed tool or running physiclaw's entrypoint: ``uv tool run`` builds a
    throwaway env with that version — downloading + preparing its wheels into
    the shared cache — and runs a no-op ``python -c pass``. ``--refresh`` so a
    just-cut release resolves."""
    return [
        uv,
        "tool",
        "run",
        "--refresh",
        "--from",
        f"physiclaw=={version}",
        "python",
        "-c",
        "pass",
    ]


def notify_staged_update() -> None:
    """Log an available update from a prior stage, without installing it.

    Skip missing/stale markers, disabled checks, or absent uv tool installs.
    """
    if _update_check_disabled():
        return
    staged = _read_staged()
    if staged is None:
        return  # nothing staged — the common case; return before any cost
    if not _is_newer(_pkg_version, staged):
        _clear_staged()  # already at/past it — drop the stale marker
        return
    uv = shutil.which("uv")
    if uv is None or not _has_tool_install(uv):
        return  # no managed tool to upgrade

    # Use the server's log stream, including its daily log file.
    log.info(
        "physiclaw %s available (you're on %s) — "
        "stop the server and run `uv tool upgrade physiclaw`.",
        staged,
        _pkg_version,
    )


def maybe_stage_update() -> None:
    """Warm the newest release in uv's cache, then record it for the next boot.

    Runs in the background; never modifies the installed tool. A failed warm
    keeps the prior marker, and an already-staged release needs no new warm.
    """
    if _update_check_disabled():
        return
    uv = shutil.which("uv")
    if uv is None:
        return
    if not _has_tool_install(uv):
        return
    latest = _fetch_pypi_version()
    if latest is None:
        return
    _write_cache(latest)
    if not _is_newer(_pkg_version, latest):
        _clear_staged()  # we're current — nothing to stage
        return
    if _read_staged() == latest:
        return  # already staged this version — don't re-download
    proc = _run(_warm_cmd(uv, latest))
    if proc is None or proc.returncode != 0:
        log.info("stage-update: warming cache for %s failed — will retry", latest)
        return  # warm failed — don't mark; retry next cycle
    if _write_staged(latest):
        log.info(
            "stage-update: physiclaw %s staged for the next startup's notice", latest
        )


def start_stage_update_thread() -> None:
    """Start staging as a daemon, without making update checks a startup gate."""
    if _update_check_disabled():
        return
    try:
        threading.Thread(
            target=maybe_stage_update, name="physiclaw-stage-update", daemon=True
        ).start()
    except RuntimeError as e:
        log.warning("could not start background update check: %s", e)
