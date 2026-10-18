"""Retention for session logs — shared by engine / runtime / macro logs.

`purge_daily_logs` covers the `<prefix>-YYYY-MM-DD.log` dailies
(CONFIG.retention.log_days); `purge_old_sessions` covers the per-session
artifact dirs (CONFIG.retention.trace_days). Two knobs because the two
sizes differ by orders of magnitude, though both default to a month. Session
writers purge at bootstrap; operational daily handlers also purge at midnight.
"""

import logging
import shutil
import time
from pathlib import Path

log = logging.getLogger(__name__)


def purge_daily_logs(dir: Path, prefix: str, days: int) -> None:
    """Expire daily logs and numbered rotation archives by mtime.

    mtime beats filename-date parsing: it tolerates clock skew and files
    appended to long after creation. Fail-open — retention must never
    take down the process doing the logging."""
    cutoff = time.time() - days * 86400
    removed = 0
    try:
        entries = list(dir.glob(f"{prefix}-*.log*"))
    except OSError:
        return
    for path in entries:
        if path.suffix != ".log" and not (
            path.suffix[1:].isdigit() and path.with_suffix("").suffix == ".log"
        ):
            continue
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            pass
    if removed:
        log.info(
            "purged %d %s daily log(s) older than %d days",
            removed,
            prefix,
            days,
        )


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
