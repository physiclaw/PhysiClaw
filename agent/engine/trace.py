"""Engine session logs — two sinks per run.

  1. `Trace` — per-day human-readable log
       log/engine/engine-YYYY-MM-DD.log
     Matches the shape of `agent/runtime/claude.py`'s _SessionLog so
     operators scan either runtime the same way. One-line summaries
     via `_summarize(event)`; internal bookkeeping events in
     `_SILENT_EVENTS` are skipped.

  2. `RawLog` — per-session structured capture
       log/engine/raw/<session-id>.jsonl
     Three line shapes:
       {"t":..., "kind":"session_start", "provider":..., "model":...,
        "prompt_hash":..., "tools":[...]}
       {"t":..., "turn":..., "kind":"request",  "messages":[...]}
       {"t":..., "turn":..., "kind":"response", "elapsed_ms":...,
        "raw": {...}}
     `session_start` fires once, before turn 0, so analysis can tell a
     hallucinated tool_call from a real one and know which model produced
     the trace. Full request messages and full provider response are kept
     afterwards, with inline base64 image payloads extracted to
     `log/engine/raw/images/<session_id>_<NNNNN>.<ext>` (sequential
     counter per session, 5-digit zero-padded; `<ext>` picked from the
     mime type — typically `.jpg`) and replaced in the jsonl by that
     relative path. Filenames sort chronologically within a session and
     don't collide across sessions. On each session bootstrap, raw
     files older than 7 days are purged so the directory stays bounded.
     Use these for prompt-engineering analysis / regression triage.
"""
import base64
import datetime as dt
import json
import logging
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_LOG_DIR = Path("log/engine")
_RAW_DIR = _LOG_DIR / "raw"
_IMAGE_DIR = _RAW_DIR / "images"

# Purge raw jsonl logs + extracted images older than this on session
# bootstrap. One week is generous for post-mortem debugging while
# keeping disk usage bounded for long-running operators.
_RETENTION_DAYS = 7

# mime → filename suffix for images extracted from data-URLs. Everything
# we actually serve is JPEG via compact.scale_image_bytes, but keep the
# fallback open for PNG / WebP in case an upstream tool starts emitting
# them.
_MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}

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


# ---------- RawLog: per-session structured capture ----------


class RawLog:
    """Per-session JSONL sink for later analysis.

    Emits `session_start` once, then one line per provider round-trip
    (request OR response). Open inside the engine's try/finally — call
    `close()` on session end.
    """

    def __init__(self, session_id: str):
        _RAW_DIR.mkdir(parents=True, exist_ok=True)
        _purge_old()
        self.session_id = session_id
        self.path = _RAW_DIR / f"{session_id}.jsonl"
        self._f = open(self.path, "a")
        self._image_counter = 0

    def write_session_start(
        self,
        *,
        provider: str,
        model: str,
        prompt_hash: str,
        tools: list[dict],
    ) -> None:
        # Tools don't change mid-session (engine builds the registry once
        # at bootstrap), so logging them once at start is sufficient and
        # keeps per-turn records lean.
        self._emit(
            "session_start",
            provider=provider, model=model,
            prompt_hash=prompt_hash, tools=tools,
        )

    def write_request(self, turn: int, messages: list[dict]) -> None:
        self._emit("request", turn=turn, messages=self._scrub_images(messages))

    def write_response(
        self, turn: int, raw: dict[str, Any], *, elapsed_ms: int,
    ) -> None:
        self._emit("response", turn=turn, elapsed_ms=elapsed_ms, raw=raw)

    def close(self) -> None:
        if not self._f.closed:
            self._f.close()

    def _emit(self, kind: str, **data: Any) -> None:
        obj = {"t": _now(), "kind": kind, **data}
        self._f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self._f.flush()

    def _persist_image(self, mime: str, b64_data: str) -> str:
        """Decode `b64_data`, write to
        `log/engine/raw/images/<session_id>_<NNNNN><ext>`, return the
        path relative to the raw log dir. The counter is per-RawLog
        instance — one per session — so filenames sort chronologically
        within a session and don't collide across sessions. Returns ""
        on decode failure so the caller can fall back to a byte-count
        stub."""
        try:
            raw = base64.b64decode(b64_data, validate=False)
        except (ValueError, TypeError):
            return ""
        self._image_counter += 1
        ext = _MIME_EXT.get(mime, ".bin")
        rel = f"images/{self.session_id}_{self._image_counter:05d}{ext}"
        path = _RAW_DIR / rel
        _IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return rel

    def _scrub_images(self, messages: list[dict]) -> list[dict]:
        """Copy of `messages` with inline base64 image data replaced by
        a reference to an on-disk file under
        `images/<session_id>_<NNNNN>.ext`. Each call gets a fresh
        counter value — no cross-request dedup, which is by design: the
        numbered sequence preserves turn order on disk for debugging.

        On decode failure, falls back to a byte-count stub so the raw
        log still distinguishes an image from a tap result."""
        out: list[dict] = []
        for m in messages:
            c = m.get("content")
            if not isinstance(c, list):
                out.append(m)
                continue
            new_c: list[dict] = []
            for b in c:
                if not isinstance(b, dict) or b.get("type") != "image_url":
                    new_c.append(b)
                    continue
                url = (b.get("image_url") or {}).get("url", "")
                if not url.startswith("data:"):
                    # Already a reference (e.g. a prior scrub ran or the
                    # upstream gave us a URL to begin with). Pass through.
                    new_c.append(b)
                    continue
                head, _, data = url.partition(",")
                mime = head[5:].partition(";")[0]  # strip "data:" and ";base64"
                rel = self._persist_image(mime, data) if data else ""
                scrubbed_url = rel or f"{head},<{len(data)}b unreadable>"
                new_c.append({
                    "type": "image_url",
                    "image_url": {"url": scrubbed_url},
                })
            out.append({**m, "content": new_c})
        return out


def _now() -> str:
    # ms precision makes per-turn latency analysis possible without having
    # to correlate against the engine log.
    return dt.datetime.now().isoformat(timespec="milliseconds")


def _purge_old(*, days: int = _RETENTION_DAYS) -> None:
    """Delete files under `log/engine/raw/` (jsonl + extracted images)
    whose mtime is older than `days`. Runs once per session bootstrap
    so the dir stays bounded even on long-running operators; mtime
    beats filename-date parsing because it tolerates clock skew and
    handles files appended to long after creation."""
    cutoff = time.time() - days * 86400
    removed = 0
    try:
        entries = list(_RAW_DIR.rglob("*"))
    except OSError:
        return
    for path in entries:
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            pass
    if removed:
        log.info("purged %d raw log file(s) older than %d days", removed, days)
