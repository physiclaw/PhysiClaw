"""Per-wake logging for the claude engine: daily narrative + session dir.

One `_SessionLog` per `claude -p` wake writes two surfaces:
  - the human-readable daily narrative, `log/claude/claude-YYYY-MM-DD.log`
    (every stream-json event summarized, images elided);
  - a per-session artifact dir, `sessions/<sid>/` with `summary.json`
    (engine schema v1 — one `physiclaw logs` / `jq` reads both engines'
    sessions) and `images/` (the screenshots the model actually saw).

Extracted from `spawn.py`, which keeps only the subprocess lifecycle;
this module owns everything written to disk about a wake.
"""

import logging
import math
import time
from collections import Counter

from physiclaw.agent.runtime.hook import Trigger
from physiclaw.agent.runtime.sentinel import STATUSES, parse_sentinel
from physiclaw.common import paths
from physiclaw.common.config import CONFIG

# Shared session-artifact surface (`common.logger.session_artifacts`) —
# the same daily writer, summary constructor, and helpers the engine's
# trace.py uses, so the claude sessions' summary.json + images/ stay
# byte-compatible (one `physiclaw logs` / `jq` reads both).
from physiclaw.common.logger import (
    DailyLogWriter,
    SessionLogSidecars,
    append_stats,
    build_summary,
    ensure_readme,
    env_snapshot,
    iso_now,
    save_image,
    write_json_atomic,
)
from physiclaw.common.logger.retention import purge_daily_logs, purge_old_sessions

log = logging.getLogger(__name__)

LOG_DIR = paths.claude_log_dir()


def _as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _text(block: dict, key: str) -> str:
    value = block.get(key)
    return value if isinstance(value, str) else ""


def _count(value) -> int:
    """Missing or malformed telemetry must not invalidate a session close."""
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _cost(value) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return number if math.isfinite(number) and number >= 0 else 0.0


def _message(data: dict) -> dict:
    """The event's `message` as a dict — {} for null/absent/non-dict.
    The `claude` CLI is external and unpinned; a shape deviation must
    cost a log line, never the wake (spawn._stream's catch is the last
    resort, this is the first)."""
    return _as_dict(data.get("message"))


def _blocks(msg: dict) -> list[dict]:
    """`msg["content"]` as a list of dict blocks — non-list content and
    non-dict blocks drop out (string content is handled by the callers
    that can render it)."""
    content = msg.get("content")
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict)]


def _redact_images(content):
    """Replace base64 image data with a length placeholder so logs stay readable."""
    if not isinstance(content, list):
        return content
    out = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "image":
            src = _as_dict(item.get("source"))
            data = _text(src, "data")
            out.append({**item, "source": {**src, "data": f"<{len(data)}b elided>"}})
        else:
            out.append(item)
    return out


# Per-session artifact dir docs — a claude-specific README (the engine's
# `SESSIONS_README` also documents events.jsonl / wire.jsonl, which this
# engine doesn't emit). summary.json shares the engine's schema v1, so the
# same `physiclaw logs` / `jq` reads both engines' sessions.
_CLAUDE_SESSIONS_README = """\
# PhysiClaw claude-code session logs

One directory per `claude -p` wake, `YYYYMMDD-HHMMSS-<6 hex digits>`. The
human-readable narrative for all wakes of a day lives alongside, in
`../claude-YYYY-MM-DD.log`. All files are UTF-8 with LF newlines on every
platform.

- `summary.json` — session metrics, schema v1 (shared with the engine's
  sessions): sid, started/ended, duration_s, model_ref, provider, triggers,
  outcome{sentinel,recap,crashed}, turns, usage{tokens,cache_hit_pct},
  cost_usd, tool_calls{name:count}, errors, images, env. Missing = the
  session was killed. Cross-session: `jq .usage.cache_hit_pct */summary.json`;
  for trends past the retention window, `log/stats.jsonl` (one summary
  line per session, both engines) survives the session-dir purge.
- `images/<HHMMSS>_<mmm>_t<turn>.<ext>` — screenshots the model saw
  (typically .jpg). Name = local capture time (hour-minute-second `_`
  milliseconds) + `_t<turn>` (the turn it belongs to), so they sort in
  capture order. Example: `104542_123_t20.jpg` = 10:45:42.123, turn 20.
- `runtime.log` — a mirror of the runtime process log stream for this
  wake (uncolored, full DEBUG for `physiclaw.*` plus INFO+ from other
  loggers): warnings and full tracebacks the daily narrative elides.
- `mcp.log` — the MCP-server process's log for this wake (camera,
  exposure/tune, gesture execution), written live by the server via an
  active-session marker. Where to look when a view came back wrong.
  Absent if the server logged nothing or was not running.

Underperforming wake? Check `images/` first — bad runs usually trace to
what the model actually saw, not how it reasoned. Look for blur, glare,
or a cropped/off-frame screen before blaming the prompt or the model.

Privacy: images/ are phone screenshots. Treat a session dir as sensitive.
"""


class _ClaudeSummary:
    """Accumulate a wake's metrics from the stream-json into the engine's
    summary.json schema (v1), so both engines' sessions read with one tool.
    Tokens, timing and cost come from the cumulative result event;
    assistant events supply distinct message ids and tool counts."""

    def __init__(
        self,
        sid: str,
        triggers: list[Trigger],
        *,
        model_ref: str,
        prompt_hash: str,
    ):
        self.sid = sid
        self.started_at = iso_now()
        self._start_mono = time.monotonic()
        self.model_ref = model_ref
        self.prompt_hash = prompt_hash
        self.triggers = [
            {"source": t.source, "description": t.description} for t in triggers
        ]
        self.sentinel: str | None = None
        self.recap = ""
        self.crashed = False
        # Tokens/cost/time come from the `result` event — the single
        # authoritative cumulative record. Per-`assistant`-event `usage` is
        # PARTIAL (streamed) and REPEATS the same `message.id` across a
        # message's content-block events, so summing it double-counts.
        self.provider_time_ms = 0
        self.cost_usd = 0.0
        self.usage: dict = {}
        self._msg_ids: set[str] = set()  # distinct assistant messages = calls
        self.tool_calls: Counter[str] = Counter()
        self.tool_errors = 0
        self.env = env_snapshot()

    def observe(self, data: dict) -> None:
        t = data.get("type")
        if t == "assistant":
            msg = _message(data)
            mid = _text(msg, "id")
            if mid:
                self._msg_ids.add(mid)
            # Each content block appears in exactly one assistant event, so
            # counting tool_use across events doesn't double-count.
            for b in _blocks(msg):
                if b.get("type") == "tool_use":
                    self.tool_calls[_text(b, "name") or "?"] += 1
        elif t == "user":
            for b in _blocks(_message(data)):
                if b.get("type") == "tool_result" and b.get("is_error"):
                    self.tool_errors += 1
        elif t == "result":
            self.provider_time_ms = _count(data.get("duration_api_ms"))
            self.cost_usd = _cost(data.get("total_cost_usd"))
            self.usage = _as_dict(data.get("usage"))

    def finalize(self, *, images: int) -> dict:
        u = self.usage
        cr = _count(u.get("cache_read_input_tokens"))
        cc = _count(u.get("cache_creation_input_tokens"))
        calls = len(self._msg_ids)
        return build_summary(
            sid=self.sid,
            started_at=self.started_at,
            duration_s=time.monotonic() - self._start_mono,
            model_ref=self.model_ref,
            prompt_hash=self.prompt_hash,
            triggers=self.triggers,
            sentinel=self.sentinel,
            recap=self.recap,
            crashed=self.crashed,
            # Distinct assistant messages = provider round-trips; on the
            # claude path one turn == one call, so both keys carry the same
            # value (engine parity — there a turn can span calls).
            turns=calls,
            provider_calls=calls,
            provider_time_ms=self.provider_time_ms,
            input_tokens=_count(u.get("input_tokens")) + cr + cc,
            output_tokens=_count(u.get("output_tokens")),
            cache_read_tokens=cr,
            cache_creation_tokens=cc,
            cost_usd=self.cost_usd,
            tool_calls=self.tool_calls,
            # Engine-internal refusal counters don't apply to the claude
            # path (Claude Code owns its own loop) — build_summary
            # renders the full key set for schema parity; populate the one
            # observable here.
            errors={"tool_errors": self.tool_errors},
            stuck_events=0,
            images=images,
            env=self.env,
        )


class _SessionLog:
    """One claude wake's logs: the daily human narrative
    (`claude-YYYY-MM-DD.log`) plus a per-session artifact dir
    (`sessions/<sid>/summary.json` + `images/`), mirroring the engine."""

    def __init__(
        self,
        sid: str,
        triggers: list[Trigger],
        *,
        model_ref: str,
        prompt_hash: str,
    ):
        purge_daily_logs(LOG_DIR, "claude", CONFIG.retention.log_days)
        self._last_text = ""  # most recent assistant text block, for sentinel check
        self._daily = DailyLogWriter(LOG_DIR, "claude")

        # Per-session artifact dir + running metrics.
        self._sdir = paths.claude_sessions_dir() / sid
        self._img_dir = self._sdir / "images"
        self._img_dir.mkdir(parents=True, exist_ok=True)
        # Per-session log sidecars: runtime.log (process log mirror) +
        # mcp.log (the server's log for this wake's window). Shared with
        # the engine writer so the two can't drift.
        self._sidecars = SessionLogSidecars(self._sdir)
        ensure_readme(paths.claude_sessions_dir(), _CLAUDE_SESSIONS_README)
        purge_old_sessions(
            paths.claude_sessions_dir(), days=CONFIG.retention.trace_days
        )
        self._summary = _ClaudeSummary(
            sid, triggers, model_ref=model_ref, prompt_hash=prompt_hash
        )
        self._turn = 0  # advanced per assistant MESSAGE; tags extracted images
        self._seen_msg_ids: set[str] = set()  # dedup streamed content-block events
        self._image_counter = 0
        self._closed = False

        sources = [t.source or "?" for t in triggers]
        self._daily.line(f"WAKE session={sid} triggers={sources}")

    def event(self, data: dict) -> dict | None:
        """Log a stream-json event. Returns the data if it's a result.

        Every event is summarized to the daily file, feeds the session
        summary, and (for tool-result screenshots) is extracted to
        images/. Assistant text additionally narrates to the runtime log.
        """
        if not isinstance(data, dict):
            return None
        if data.get("type") == "assistant":
            # One assistant MESSAGE streams as several assistant EVENTS
            # sharing message.id; advance the turn only on a new message,
            # so image tags match summary.json's message-id-deduped turn
            # count (summary counts `len(_msg_ids)` — mirror it here).
            # Fall back to per-event advance only when there is no id to
            # dedup on (never in a real Claude stream).
            msg = _message(data)
            mid = _text(msg, "id")
            if not mid:
                self._turn += 1
            elif mid not in self._seen_msg_ids:
                self._seen_msg_ids.add(mid)
                self._turn += 1
            # Sentinel evidence for done(): capture assistant text here
            # (not in the render-only _summarize) — the LAST non-empty
            # text block wins, whichever event carried it. String-shaped
            # content counts too, so a shape drift can't cost the sentinel.
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                self._last_text = content
            for b in _blocks(msg):
                text = _text(b, "text")
                if b.get("type") == "text" and text.strip():
                    self._last_text = text
        summary = self._summarize(data)
        if summary:
            self._daily.line(summary)
        self._summary.observe(data)
        self._extract_images(data)
        self._forward_to_runtime(data)
        return data if data.get("type") == "result" else None

    def raw(self, text: str) -> None:
        self._daily.line(f"raw: {text[:500]}")

    def done(self, returncode: int | str) -> str:
        """Write OUTCOME + EXIT bookends, record them on the summary, and
        return the OUTCOME status.

        Trust the sentinel only when the process exited cleanly (code 0);
        otherwise the run crashed even if the agent claimed DONE earlier.
        """
        last_line = next(
            (line for line in reversed(self._last_text.splitlines()) if line.strip()),
            "",
        )
        status, recap = parse_sentinel(last_line) if returncode == 0 else (None, "")
        if not status:
            status = "UNDONE"
            recap = (last_line or "(no text)").strip()[:200]
        self._summary.sentinel = status if status in STATUSES else None
        self._summary.recap = recap
        self._summary.crashed = returncode != 0
        self._daily.line(f"OUTCOME: {status} - {recap}")
        self._daily.line(f"EXIT code={returncode}")
        self._daily.footer()
        return status

    def close(self) -> None:
        """Finalize summary.json, then close the daily-log handle. Idempotent
        and OSError-safe — a full disk must not turn a DONE wake into a crash."""
        if self._closed:
            return
        self._closed = True
        try:
            summary = self._summary.finalize(images=self._image_counter)
            write_json_atomic(self._sdir / "summary.json", summary)
            append_stats(paths.LOG_DIR, summary)
        except OSError:
            log.warning("claude session summary write failed", exc_info=True)
        finally:
            self._sidecars.close()
            self._daily.close()

    def _extract_images(self, data: dict) -> None:
        """Decode base64 screenshots from tool_result blocks to
        images/<HHMMSS>_<mmm>_t<turn>.<ext> — the screenshots the model
        actually saw, the biggest post-mortem win over the elided daily log."""
        if data.get("type") != "user":
            return
        for b in _blocks(_message(data)):
            if b.get("type") != "tool_result":
                continue
            content = b.get("content")
            if not isinstance(content, list):
                continue
            for c in content:
                if not isinstance(c, dict) or c.get("type") != "image":
                    continue
                src = _as_dict(c.get("source"))
                b64 = _text(src, "data")
                if src.get("type") == "base64" and b64:
                    self._save_image(_text(src, "media_type") or "image/jpeg", b64)

    def _save_image(self, mime: str, b64: str) -> None:
        try:
            name = save_image(self._img_dir, self._turn, mime, b64)
        except OSError:
            return  # don't advance the counter for a write that didn't land
        if name is not None:
            self._image_counter += 1

    def _forward_to_runtime(self, data: dict) -> None:
        """Forward the high-signal subset of events to runtime stderr so
        the daemon log is followable without tailing the detail file.

        Only assistant TEXT blocks are forwarded — tool_use / tool_result
        are already visible in the MCP server's own log, and the final
        `result` event is logged from `spawn_claude`'s exit path.
        """
        if data.get("type") != "assistant":
            return
        for b in _blocks(_message(data)):
            text = _text(b, "text")
            if b.get("type") == "text" and text.strip():
                first = text.strip().splitlines()[0][:200]
                log.info("claude: %s", first)
                return

    def _summarize(self, data: dict) -> str | None:
        """Render one stream-json event as a daily-log line (or None).
        Pure rendering — sentinel evidence is captured in `event()`."""
        t = data.get("type", "")

        if t == "assistant":
            msg = _message(data)
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                return f"text: {content[:1000]}"
            parts = []
            for b in _blocks(msg):
                if b.get("type") == "tool_use":
                    parts.append(
                        f"tool_use: {b.get('name', '?')} {str(b.get('input', ''))[:1000]}"
                    )
                elif b.get("type") == "text" and _text(b, "text").strip():
                    parts.append(f"text: {_text(b, 'text')[:1000]}")
                elif b.get("type") == "thinking" and _text(b, "thinking").strip():
                    parts.append(f"thinking: {_text(b, 'thinking')[:2000]}")
            return " | ".join(parts) if parts else None

        if t == "user":
            for b in _blocks(_message(data)):
                if b.get("type") == "tool_result":
                    return f"tool_result: {str(_redact_images(b.get('content', '')))[:1000]}"

        if t == "result":
            return f"result: turns={data.get('num_turns', '?')} {str(data.get('result', ''))[:2000]}"

        return None
