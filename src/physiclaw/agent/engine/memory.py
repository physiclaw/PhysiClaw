"""Memory retrieval + update for the engine.

Read paths:
  - `load_owner` — `memory/OWNER.md`. Routed through the doctrine renderer
    so it appears as a slot inside `# Doctrine`.
  - `load_persistent` — `memory/memory.md`. Auto-injected at session start.
  - `load_recent_entries` — last N entries across `memory/YYYY-MM-DD.md`
    daily logs, walking back through prior days when the latest file
    has fewer than N. Fetched on demand via `read_logs`; injecting
    them every wake would burn context.

Write paths used by the local tool handlers: `append_log`, `save_fact`,
`update_fact`.
"""
import datetime as dt
import re

from physiclaw import paths
from physiclaw.config import CONFIG

MEMORY_DIR = paths.memory_dir()
MEMORY_FILE = MEMORY_DIR / "memory.md"
OWNER_FILE = MEMORY_DIR / "OWNER.md"

# `read_logs` default. Counts entry lines (one log step), not files —
# a low-activity recent day spills into older days until N is reached.
# Owner-tunable via `[memory] default_log_entries` in config.toml.
DEFAULT_LOG_ENTRIES = CONFIG.memory.default_log_entries

# Hard ceiling on how far back to scan when collecting entries. Guards
# against an indefinite loop on a near-empty memory dir; missing days
# are skipped cheaply so the cost is just calendar arithmetic.
_LOOKBACK_DAYS_CEILING = 365

# `append_log` writes `[HH:MM] …`. `read_logs` rewrites the prefix to
# `[YYYY-MM-DD HH:MM] …` using the file's date so a merged-day view
# stays unambiguous about cross-day order.
_TIME_PREFIX_RE = re.compile(r"^\[(\d{2}:\d{2})\]\s*(.*)$")


def load_owner() -> str:
    """`memory/OWNER.md` body, or "" if missing/empty. Called by the
    doctrine renderer for the OWNER.md slot."""
    if not OWNER_FILE.exists():
        return ""
    return OWNER_FILE.read_text().strip()


def load_persistent() -> str:
    """`memory/memory.md` body, or "" if missing/empty. Auto-injected into
    the SYSTEM prompt under the engine-rendered `## memory.md` heading
    — no inner wrapper here. (Spec lives separately in the
    PERSISTENCE.md doctrine slot.)"""
    if not MEMORY_FILE.exists():
        return ""
    return MEMORY_FILE.read_text().strip()


def load_recent_entries(n: int = DEFAULT_LOG_ENTRIES) -> str:
    """Last N log entries across daily files, most recent first.

    Walks `memory/YYYY-MM-DD.md` from today backward, accumulating
    non-empty content lines. If today's file has fewer than N entries,
    yesterday's is read too, and so on, up to `_LOOKBACK_DAYS_CEILING`.
    The `# YYYY-MM-DD` header line and blank lines are skipped — only
    actual entries count toward N.

    Each line's `[HH:MM]` prefix is rewritten to `[YYYY-MM-DD HH:MM]`
    using the file's date so the merged stream is unambiguous about
    when each entry happened. Lines without a time prefix pass through
    unchanged.

    Returns "" if no entries are found within the ceiling.
    """
    today = dt.date.today()
    collected: list[str] = []
    for i in range(_LOOKBACK_DAYS_CEILING):
        if len(collected) >= n:
            break
        d = today - dt.timedelta(days=i)
        p = MEMORY_DIR / f"{d.isoformat()}.md"
        try:
            text = p.read_text()
        except FileNotFoundError:
            continue
        # Files are append-order (oldest line first). Reverse before
        # taking from a single file so the most recent line in that
        # file appears first in the merged output.
        per_file: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            per_file.append(_stamp_date(stripped, d))
        for line in reversed(per_file):
            collected.append(line)
            if len(collected) >= n:
                break
    if not collected:
        return ""
    return "\n".join(collected)


def _stamp_date(line: str, d: dt.date) -> str:
    m = _TIME_PREFIX_RE.match(line)
    if not m:
        return line
    return f"[{d.isoformat()} {m.group(1)}] {m.group(2)}"


def append_log(entry: str) -> None:
    """Append a log line to today's daily file. Creates the file if needed."""
    entry = entry.strip()
    if not entry:
        return
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    path = MEMORY_DIR / f"{dt.date.today().isoformat()}.md"
    is_new = not path.exists()
    with open(path, "a") as f:
        if is_new:
            f.write(f"# {dt.date.today().isoformat()}\n\n")
        f.write(entry + "\n")


def save_fact(text: str) -> None:
    """Append a persistent fact to memory.md."""
    text = text.strip()
    if not text:
        return
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    with open(MEMORY_FILE, "a") as f:
        f.write(text + "\n")


def update_fact(old: str, new: str) -> None:
    """Replace the single occurrence of `old` with `new` in memory.md.
    Empty `new` deletes the line containing `old`.

    Raises FileNotFoundError if memory.md is absent. Raises ValueError if
    `old` is not found OR appears more than once — the caller must pick a
    substring specific enough to match exactly one place.
    """
    if not MEMORY_FILE.exists():
        raise FileNotFoundError(f"{MEMORY_FILE} does not exist")
    text = MEMORY_FILE.read_text()
    count = text.count(old)
    if count == 0:
        raise ValueError(f"old text not found in memory.md: {old!r}")
    if count > 1:
        raise ValueError(
            f"old text matched {count} places in memory.md — "
            f"narrow the string so it matches exactly once"
        )
    if new:
        updated = text.replace(old, new, 1)
    else:
        lines = text.splitlines(keepends=True)
        out: list[str] = []
        removed = False
        for line in lines:
            if not removed and old in line:
                removed = True
                continue
            out.append(line)
        updated = "".join(out)
    MEMORY_FILE.write_text(updated)
