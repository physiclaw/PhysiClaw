"""Per-day human-readable log for engine sessions.

One file per day, shared across all sessions — matches the shape of
`log/claude/claude-YYYY-MM-DD.log` so operators can scan either runtime's
trail the same way.

  log/engine/engine-YYYY-MM-DD.log

Format per line:
  [HH:MM:SS] <summary>

Sessions bookended by `============` banners and WAKE / OUTCOME lines.
Long values (tool args, result content, image data) are truncated.
Image base64 is replaced with a `<image Nb>` shape marker.

`Trace.write(event: dict)` takes the same event shape the previous
JSONL writer accepted — `_summarize()` maps each event type to a single
log line. Unknown events fall back to a compact repr so nothing gets
dropped silently.
"""
import datetime as dt
from pathlib import Path
from typing import Any

_LOG_DIR = Path("log/engine")

# Events that are internal bookkeeping — don't surface in the human log.
# Add here when silencing a new event is cheaper than adding a dedicated
# summary branch.
_SILENT_EVENTS = frozenset({"prefix_pinned", "finish_length_warning"})


# ---------- public formatting helpers (shared with engine.py) ----------


def brief(value: Any, limit: int = 80) -> str:
    """One-line truncated repr for log output."""
    s = value if isinstance(value, str) else repr(value)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def brief_args(args: dict[str, Any]) -> str:
    return ", ".join(f"{k}={brief(v, 40)}" for k, v in args.items())


def brief_content(content: Any) -> str:
    """Compact summary of a ToolResult.content or MCP blocks list."""
    if isinstance(content, str):
        return brief(content, 80)
    if not isinstance(content, list):
        return brief(repr(content), 80)
    parts: list[str] = []
    for b in content:
        if not isinstance(b, dict):
            parts.append("?")
            continue
        t = b.get("type")
        if t == "text":
            parts.append(brief(b.get("text", ""), 80))
        elif t == "image":
            data = b.get("data", "")
            parts.append(f"<image {len(data)}b>")
        elif t == "image_url":
            url = (b.get("image_url") or {}).get("url", "")
            _, _, data = url.partition(",")
            parts.append(f"<image {len(data)}b>")
        else:
            parts.append(t or "?")
    return " + ".join(parts) or "(empty)"


# ---------- Trace ----------


class Trace:
    def __init__(self, session_id: str):
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.session_id = session_id
        self._date = dt.datetime.now().strftime("%Y-%m-%d")
        self._f = open(_LOG_DIR / f"engine-{self._date}.log", "a")
        self._f.write(f"\n{'=' * 60}\n")
        self._f.flush()

    def write(self, event: dict[str, Any]) -> None:
        msg = _summarize(event)
        if msg is None:
            return
        self._emit(msg)

    def close(self) -> None:
        if not self._f.closed:
            self._f.close()

    def _emit(self, msg: str) -> None:
        now = dt.datetime.now()
        today = now.strftime("%Y-%m-%d")
        if today != self._date:
            # Crossed midnight — close current file, continue in today's.
            self._f.write(f"[{now:%H:%M:%S}] ROLLOVER → engine-{today}.log\n")
            self._f.flush()
            self._f.close()
            self._date = today
            self._f = open(_LOG_DIR / f"engine-{today}.log", "a")
            self._f.write(
                f"\n[{now:%H:%M:%S}] ROLLOVER ← continued from previous day\n"
            )
        self._f.write(f"[{now:%H:%M:%S}] {msg}\n")
        self._f.flush()


# ---------- event → one-line summary ----------


def _summarize(event: dict[str, Any]) -> str | None:  # noqa: C901 — flat dispatch
    name = event.get("event", "")
    t = event.get("turn")
    pfx = f"turn {t}: " if t is not None else ""

    if name == "wake":
        triggers = event.get("triggers") or []
        sources = [x.get("source") or "?" for x in triggers]
        return (
            f"WAKE session={event.get('session','?')} "
            f"provider={event.get('provider','?')} triggers={sources}"
        )
    if name == "tools_loaded":
        return (
            f"tools: {len(event.get('mcp') or [])} MCP + "
            f"{len(event.get('local') or [])} local"
        )
    if name == "request":
        return f"{pfx}request ({event.get('message_count','?')} messages)"
    if name == "response":
        calls = [c.get("name") for c in event.get("tool_calls") or []]
        return f"{pfx}response finish={event.get('finish_reason','?')} calls={calls}"
    if name == "tool_result":
        args = brief_args(event.get("arguments") or {})
        if "text" in event:
            result = brief(event["text"], 80)
        else:
            result = brief_content(event.get("blocks") or [])
        return f"{pfx}{event.get('name','?')}({args}) → {result}"
    if name == "tool_invalid_args":
        return f"{pfx}{event.get('name','?')} invalid args: {brief(event.get('error',''), 200)}"
    if name == "tool_unknown":
        return f"{pfx}{event.get('name','?')} unknown tool"
    if name == "tool_error":
        return f"{pfx}{event.get('name','?')} failed: {brief(event.get('error',''), 200)}"
    if name == "violations":
        return f"{pfx}violations {event.get('codes') or []}"
    if name == "log_append":
        return f"{pfx}log: {brief(event.get('entry',''), 200)}"
    if name == "memory_save":
        return f"{pfx}memory: {brief(event.get('text',''), 200)}"
    if name == "cron_create":
        job = event.get("job") or {}
        return (
            f"{pfx}create_cron id={job.get('id')} "
            f"schedule={job.get('schedule')!r}"
        )
    if name == "cron_create_error":
        return f"{pfx}create_cron failed: {brief(event.get('error',''), 200)}"
    if name == "sentinel":
        return (
            f"{pfx}SENTINEL {event.get('name','?')} — "
            f"{event.get('recap','')}"
        )
    if name == "wait_auto_scheduled":
        return (
            f"WAIT auto-scheduled: {event.get('job_id')} "
            f"at {event.get('at')}"
        )
    if name == "wait_auto_schedule_failed":
        return f"WAIT auto-schedule failed: {event.get('error','?')}"
    if name == "done":
        return (
            f"OUTCOME: {event.get('sentinel') or '(none)'} — "
            f"{event.get('recap','')}"
        )
    if name == "crashed":
        return "CRASHED"
    if name == "provider_failed":
        return f"{pfx}provider failed: {brief(event.get('error',''), 200)}"
    if name == "prefix_drift":
        return (
            f"{pfx}!! PREFIX DRIFT "
            f"expected={event.get('expected','')[:12]}… "
            f"actual={event.get('actual','')[:12]}…"
        )
    if name in _SILENT_EVENTS:
        return None
    # Fallback — compact repr so nothing disappears silently.
    return f"event {name}: {brief(repr(event), 200)}"
