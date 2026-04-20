"""Memory retrieval + update for the engine.

`load_context` snapshots `memory/memory.md` plus the last 3 daily logs into a
markdown block injected into the SYSTEM prompt at turn 0. `append_log` and
`save_fact` are called from the engine loop when the model emits `log_entry`
or `memory_save` in its JSON response.
"""
import datetime as dt
from pathlib import Path

MEMORY_DIR = Path("memory")
MEMORY_FILE = MEMORY_DIR / "memory.md"
_DAILY_LOOKBACK = 3


def load_context() -> str:
    """Return a markdown block: persistent memory + last 3 days of logs."""
    sections: list[str] = []

    if MEMORY_FILE.exists():
        body = MEMORY_FILE.read_text().strip()
        if body:
            sections.append(f"## Persistent memory\n\n{body}")

    dailies: list[str] = []
    today = dt.date.today()
    for i in range(_DAILY_LOOKBACK):
        d = today - dt.timedelta(days=i)
        p = MEMORY_DIR / f"{d.isoformat()}.md"
        if not p.exists():
            continue
        body = p.read_text().strip()
        if not body:
            continue
        dailies.append(body if body.startswith("#") else f"### {d.isoformat()}\n\n{body}")
    if dailies:
        sections.append("## Recent activity (last 3 days)\n\n" + "\n\n".join(dailies))

    return "\n\n".join(sections)


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
