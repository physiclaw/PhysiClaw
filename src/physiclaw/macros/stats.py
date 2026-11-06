"""Per-macro run counters — ``~/.physiclaw/macros/stats.json``.

One machine-written JSON file keyed by macro name (the job-stats
convention: total_runs / total_successes / total_aborts, last_run_at /
last_success_at, and consecutive_aborts — the re-rehearse signal: a
lifetime ratio says the macro is good, a consecutive streak says the app
layout changed under it). Every run records through
`runner.run_and_record` — engine and CLI rehearsal alike; read only by
`physiclaw macros stats` — never rendered into the prompt, which must
stay byte-stable across wakes.

Never fatal in either direction: a missing or invalid JSON file loads as
empty. Malformed entries and counters reset without discarding valid
history. Writers hold a bounded process lock across loading, folding and
atomic replacement so concurrent runs do not erase each other's counters.
Keys whose macro file is gone are pruned at write time. Additive
fields load as absent — no schema versioning (alpha convention).
"""

import datetime as dt
import json
import logging
from pathlib import Path

from physiclaw.common import paths
from physiclaw.common.logger import write_json_atomic
from physiclaw.common.text import read_text
from physiclaw.macros.model import REASON_BAD_INPUT
from physiclaw.port.locks import FileLockBusy, exclusive_file_lock

log = logging.getLogger(__name__)

_COUNTERS = {
    "total_runs": 0,
    "total_successes": 0,
    "total_aborts": 0,
    "consecutive_aborts": 0,
}
_LOCK_TIMEOUT_SECONDS = 1.0


def stats_file() -> Path:
    return paths.macros_dir() / "stats.json"


def load() -> dict[str, dict]:
    """Recorded stats with mapping entries and nonnegative integer counters.

    Normalize at the read boundary so both recording and the CLI can use
    a partially corrupt file. Valid history and additive fields survive.
    """
    path = stats_file()
    if not path.exists():
        return {}
    try:
        data = json.loads(read_text(path))
    except (OSError, ValueError):
        log.warning("macro stats unreadable; starting fresh", exc_info=True)
        return {}
    if not isinstance(data, dict):
        return {}
    for name, raw in data.items():
        entry = raw if isinstance(raw, dict) else {}
        for counter in _COUNTERS:
            value = entry.get(counter, 0)
            entry[counter] = value if type(value) is int and value >= 0 else 0
        if "last_abort" in entry and not isinstance(entry["last_abort"], dict):
            del entry["last_abort"]
        data[name] = entry
    return data


def record(
    name: str,
    *,
    ok: bool,
    known_names: set[str],
    step: int | None = None,
    reason: str | None = None,
    detail: str = "",
    run_id: str | None = None,
) -> None:
    """Fold one run outcome into the file. `known_names` (the currently
    existing macro names) prunes orphan keys so deleted macros don't leave
    stats behind. Best-effort: a write failure logs and moves on — stats
    must never take down a session."""
    try:
        path = stats_file()
        # Lock the stable sibling rather than the atomically replaced JSON.
        with exclusive_file_lock(
            path.with_suffix(".lock"), timeout=_LOCK_TIMEOUT_SECONDS
        ):
            data = load()
            # Qualified `app/name` keys are pack macros: their files live under
            # playbooks/, not macros/, so the user-macro prune set can never
            # vouch for them — keep them unconditionally (a deleted pack leaves
            # a few orphan lines, which beats losing the decay signal).
            data = {
                k: v
                for k, v in data.items()
                if k in known_names or k == name or "/" in k
            }
            entry = data.setdefault(name, dict(_COUNTERS))
            now = dt.datetime.now().isoformat(timespec="seconds")
            entry["total_runs"] += 1
            entry["last_run_at"] = now
            if ok:
                entry["total_successes"] += 1
                entry["consecutive_aborts"] = 0
                entry["last_success_at"] = now
            else:
                entry["total_aborts"] += 1
                # Bad input never reached the screen: count the abort without
                # changing the streak that signals a changed app layout.
                if reason != REASON_BAD_INPUT:
                    entry["consecutive_aborts"] += 1
                entry["last_abort"] = {
                    "ts": now,
                    "step": step,
                    "reason": reason,
                    "detail": detail,
                    # The forensic pointer to this abort's per-step trail.
                    "run": run_id,
                }
            write_json_atomic(path, data)
    except (OSError, FileLockBusy):
        log.warning("macro stats update failed", exc_info=True)
