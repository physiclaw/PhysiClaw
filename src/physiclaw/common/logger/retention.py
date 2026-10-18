"""Retention for session logs — shared by engine / runtime / macro logs.

`purge_log_archives` covers the numbered service-log archives
(CONFIG.retention.log_days); `purge_old_sessions` covers the per-session
artifact dirs (CONFIG.retention.trace_days). Two knobs because the two
sizes differ by orders of magnitude, though both default to a month. Session
writers purge at bootstrap; service handlers also purge at midnight.
"""

import shutil
import time
from pathlib import Path


def purge_log_archives(dir: Path, prefix: str, days: int, *, backup_count: int) -> int:
    """Expire numbered archives by age or backup count; return the count removed.

    mtime beats filename-date parsing: it tolerates clock skew and files
    appended to long after creation. The caller holds the service's file
    lock so rotation cannot replace an archive between stat and unlink.
    Fail-open — retention must never take down the process doing the logging.
    """
    cutoff = time.time() - days * 86400
    removed = 0
    stem = f"{prefix}.log."
    try:
        entries = list(dir.glob(f"{stem}*"))
    except OSError:
        return 0
    for path in entries:
        number = path.name.removeprefix(stem)
        if not number.isdecimal():
            continue
        try:
            if path.is_file() and (
                int(number) > backup_count or path.stat().st_mtime < cutoff
            ):
                path.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def purge_old_sessions(sessions_dir: Path, *, days: int) -> int:
    """Remove session dirs under `sessions_dir` whose newest file is older
    than `days` (mtime, not filename — tolerant of clock skew + files
    appended long after creation). Fail-open; returns the count removed.
    Used by engine and macro session writers."""
    cutoff = time.time() - days * 86400
    try:
        dirs = [d for d in sessions_dir.iterdir() if d.is_dir() and not d.is_symlink()]
    except OSError:
        return 0
    removed = 0
    for d in dirs:
        try:
            newest = max(
                (p.stat().st_mtime for p in d.rglob("*") if p.is_file()),
                default=d.stat().st_mtime,
            )
            if newest < cutoff:
                shutil.rmtree(d)
                removed += 1
        except OSError:
            pass
    return removed
