"""`RawLog` — per-session capture of the provider round-trips."""

import json
import logging
from dataclasses import asdict
from typing import Any

from physiclaw.agent.trace import store
from physiclaw.common.logger import iso_now
from physiclaw.contract import wire
from physiclaw.contract.dto import MicroRecord

log = logging.getLogger(__name__)


class RawLog:
    """Per-session JSONL sink for later analysis.

    Emits `session_start` once, then one line per provider round-trip
    (request OR response). Open inside the engine's try/finally — call
    `close()` on session end.
    """

    def __init__(self, session_id: str, images: "store.Images"):
        d = store._session_dir(session_id)
        # The session's frame store, the trace's too: a frame filed at
        # its tool result is what a request's scrub references.
        self._images = images
        store._purge_old()
        self.session_id = session_id
        self.path = d / "wire.jsonl"
        self._f = open(self.path, "a", encoding="utf-8", newline="\n")

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
            provider=provider,
            model=model,
            prompt_hash=prompt_hash,
            tools=tools,
        )

    def write_request(self, turn: int, messages: list[dict]) -> None:
        # Inline frames become session-relative paths: the file the
        # frame's own tool result filed, or one filed now under this turn.
        scrubbed = wire.scrub_messages(
            messages, lambda mime, b64: self._images.put(turn, mime, b64)
        )
        self._emit("request", turn=turn, messages=scrubbed)

    def write_response(
        self,
        turn: int,
        raw: dict[str, Any],
        *,
        elapsed_ms: int,
        synthesized: bool = False,
    ) -> None:
        # Wire fidelity: a synthesized response was composed by the
        # conductor — nothing was sent to the provider, and no request
        # record precedes it (the loop skips the request write entirely).
        extra = {"synthesized": True} if synthesized else {}
        self._emit("response", turn=turn, elapsed_ms=elapsed_ms, **extra, raw=raw)

    def write_micro(self, rec: MicroRecord) -> None:
        """One conductor decision call in a single record (kind "micro")
        — self-contained, so `playbooks micro` re-asks it without the
        walk. Text only, so no scrubbing pass."""
        self._emit("micro", **asdict(rec))

    def close(self) -> None:
        if not self._f.closed:
            self._f.close()

    def _emit(self, kind: str, **data: Any) -> None:
        """Fail-open like the sibling sinks (`Trace._write_event`,
        `DailyLogWriter.line`): a full disk or an unserializable payload
        costs the wire record, never the session."""
        obj = {"t": iso_now(), "kind": kind, **data}
        try:
            self._f.write(json.dumps(obj, ensure_ascii=False) + "\n")
            self._f.flush()
        except (OSError, TypeError, ValueError):
            log.warning("wire.jsonl write failed", exc_info=True)
