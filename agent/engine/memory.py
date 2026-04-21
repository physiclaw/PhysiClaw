"""Memory retrieval + update for the engine.

Read paths:
  - `load_owner` — `memory/OWNER.md`. Routed through the doctrine renderer
    so it appears as a slot inside `# Doctrine`.
  - `load_persistent` — `memory/memory.md`. Auto-injected at session start.
  - `load_recent_activity` — last N `memory/YYYY-MM-DD.md` daily logs.
    Fetched on demand via `read_memory`; injecting them every wake would
    burn context in days.
  - `load_context` — persistent + recent, used by `read_memory`.

Write paths used by the local tool handlers: `append_log`, `save_fact`,
`update_fact`.
"""
import datetime as dt
from pathlib import Path

MEMORY_DIR = Path("memory")
MEMORY_FILE = MEMORY_DIR / "memory.md"
OWNER_FILE = MEMORY_DIR / "OWNER.md"
_DAILY_LOOKBACK = 3


def load_owner() -> str:
    """`memory/OWNER.md` body, or "" if missing/empty. Called by the
    doctrine renderer for the OWNER.md slot."""
    if not OWNER_FILE.exists():
        return ""
    return OWNER_FILE.read_text().strip()


def load_persistent() -> str:
    """`memory/memory.md` body, or "" if missing/empty. Auto-injected into
    the SYSTEM prompt under the engine-rendered `## Memory` heading — no
    inner wrapper here."""
    if not MEMORY_FILE.exists():
        return ""
    return MEMORY_FILE.read_text().strip()


def load_recent_activity(lookback_days: int = _DAILY_LOOKBACK) -> str:
    """Last N daily logs as a single markdown block, or "" if none.

    Fetched on demand via the `read_memory` tool — NOT auto-injected at
    session start. The model decides when it needs recent context."""
    today = dt.date.today()
    dailies: list[str] = []
    for i in range(lookback_days):
        d = today - dt.timedelta(days=i)
        p = MEMORY_DIR / f"{d.isoformat()}.md"
        if not p.exists():
            continue
        body = p.read_text().strip()
        if not body:
            continue
        dailies.append(
            body if body.startswith("#") else f"### {d.isoformat()}\n\n{body}"
        )
    if not dailies:
        return ""
    return f"## Recent activity (last {lookback_days} days)\n\n" + "\n\n".join(dailies)


def load_context() -> str:
    """Persistent memory + recent daily logs. Returned by the `read_memory`
    tool when the model wants the full bundle on demand. Inner headings
    here help the tool output stay parseable; the prompt-injected path
    uses `load_persistent` directly without the wrapper."""
    parts: list[str] = []
    p = load_persistent()
    if p:
        parts.append(f"## Persistent memory\n\n{p}")
    r = load_recent_activity()
    if r:
        parts.append(r)
    return "\n\n".join(parts)


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
