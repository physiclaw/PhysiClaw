"""Opt-in learned lessons stored in `learned/pitfalls/pitfalls.md`.

When enabled, assembly injects the list and exposes `add_pitfall`; a long DONE
prompts capture, then a separate curator consolidates the list. These are
model-authored claims, not verified fixes. Writes deduplicate and cap entries,
with full snapshots in `history.jsonl`. Disabling leaves stored data intact.
"""

import datetime as dt
import json
import re
from typing import TYPE_CHECKING

from physiclaw.agent.runtime.sentinel import DONE
from physiclaw.common import paths
from physiclaw.common.config import CONFIG
from physiclaw.common.text import append_line, read_text, write_text_atomic

if TYPE_CHECKING:
    from physiclaw.agent.engine.session import Session

_FILE = "pitfalls.md"
_HISTORY = "history.jsonl"
_HEADER = "## Learned pitfalls"
_DOCTRINE = (
    "Lessons from past runs; use only those relevant to this app. "
    "Re-ground any mentioned coordinates from the current view before acting."
)
_BULLET_RE = re.compile(r"^\s*-\s+(.*\S)\s*$")


def _file():
    return paths.pitfalls_dir() / _FILE


def _norm(text: str) -> str:
    """Dedup key: whitespace-collapsed, case-folded."""
    return " ".join(text.split()).casefold()


def _clean(item: str) -> str:
    """One-line, whitespace-collapsed, clamped to `max_item_chars`. "" if blank."""
    s = " ".join((item or "").split())
    cap = CONFIG.pitfalls.max_item_chars
    if len(s) > cap:
        s = s[: cap - 1].rstrip() + "…"
    return s


def _dedup(items: list[str]) -> list[str]:
    """Drop blanks and normalized-duplicates, first wins, order preserved."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in items:
        s = _clean(raw)
        key = _norm(s)
        if not s or key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def read() -> list[str]:
    """The current list, newest first (file order). [] if none."""
    path = _file()
    if not path.exists():
        return []
    out: list[str] = []
    for line in read_text(path).splitlines():
        m = _BULLET_RE.match(line)
        if m:
            out.append(m.group(1))
    return out


def _append_history(op: str, items: list[str]) -> None:
    path = paths.pitfalls_dir() / _HISTORY
    record = {
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
        "op": op,
        "items": items,
    }
    append_line(path, json.dumps(record, ensure_ascii=False))


def _commit(op: str, items: list[str]) -> None:
    """Persist `items` (already deduped + capped) as the new list + snapshot."""
    path = _file()
    path.parent.mkdir(parents=True, exist_ok=True)
    _append_history(op, items)
    write_text_atomic(path, "".join(f"- {x}\n" for x in items))


def add(items: list[str]) -> dict:
    """Prepend up to 3 new pitfalls (newest on top), deduped against the batch
    and the existing list, each clamped. Hard-cut to `max_items` from the
    bottom (oldest) as a backstop — the curator does the real consolidation.
    Surviving existing items retain their text and order. Returns {added, total},
    counting only new items that remain after enforcing the cap."""
    fresh = _dedup(items or [])[:3]
    existing = read()
    existing_keys = {_norm(x) for x in existing}
    fresh = [x for x in fresh if _norm(x) not in existing_keys]
    if not fresh:
        return {"added": 0, "total": len(existing)}
    merged = (fresh + existing)[: CONFIG.pitfalls.max_items]
    _commit("add", merged)
    return {"added": min(len(fresh), len(merged)), "total": len(merged)}


def replace(items: list[str]) -> dict:
    """Curator write path: replace the whole list with `items`, deduped and
    hard-cut to the top `max_items` (drop oldest). No-op guard: an empty result
    never wipes a non-empty list. Returns {total, dropped}."""
    cleaned = _dedup(items or [])
    if not cleaned:
        return {"total": len(read()), "dropped": 0}
    dropped = max(0, len(cleaned) - CONFIG.pitfalls.max_items)
    kept = cleaned[: CONFIG.pitfalls.max_items]
    _commit("curate", kept)
    return {"total": len(kept), "dropped": dropped}


def render_section(items: list[str] | None = None) -> str:
    """The `## Learned pitfalls` block for SYSTEM (after user skills), or "" when
    the list is empty. Built once at bootstrap; pass an already-read list to
    avoid re-reading the file (the caller also needs the count)."""
    if items is None:
        items = read()
    if not items:
        return ""
    return "\n".join([_HEADER, "", _DOCTRINE, "", *(f"- {x}" for x in items)])


def should_capture(status: str, turn: int, session: "Session") -> tuple[bool, str]:
    """Whether to force `add_pitfall(...)` before this close, plus a diagnostic
    seed. When enabled, capture on DONE at or above `capture_turn_floor`.
    Other closing statuses never trigger capture."""
    if (
        not CONFIG.pitfalls.enabled
        or status != DONE
        or turn < CONFIG.pitfalls.capture_turn_floor
    ):
        return False, ""
    seed = f"{turn} turns"
    if session.stuck_events:
        seed += f", loop-guard fired {session.stuck_events}×"
    seed += ", closing DONE"
    return True, seed


def corrective(seed: str, trajectory: str = "") -> str:
    """The pre-close corrective injected on a rejected long-DONE `end_session`.
    Names the exact re-issue shape, the measured-waste seed, and — when present
    — the turn-marked plan/scratchpad trajectory to mine for the real traps."""
    msg = (
        "Rejected — before closing, `add_pitfall(...)` with the traps that "
        f"wasted turns this session (up to 3; session: {seed}). Each: lead with "
        "the app, then `trap → the fix that worked`, terse. If nothing was worth "
        "banking, pass an empty list. Then re-issue `end_session`."
    )
    if trajectory:
        msg += (
            "\n\nMine your run for the turn-wasters — where the plan churned or "
            "the same action kept failing:"
            f"\n{trajectory}"
        )
    return msg
