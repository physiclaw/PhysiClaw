"""`RawLog` — per-session capture of the provider round-trips."""

import json
import logging
from dataclasses import asdict, fields
from typing import Any, Literal

from physiclaw.agent.trace import store
from physiclaw.common.logger import iso_now
from physiclaw.contract import wire
from physiclaw.contract.dto import MicroRecord, UsageCall
from physiclaw.contract.plugin import TurnProducer

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
        self.path = d / "model.jsonl"
        self._f = open(self.path, "a", encoding="utf-8", newline="\n")
        # The turn the latest request/response record carried: a micro
        # record files an unseen frame under it (its frame is normally
        # one a tool result already filed, so the store returns that
        # file; this only names a first sighting).
        self._turn = 0

    def write_session_start(
        self,
        *,
        provider: str,
        model: str,
        prompt_hash: str,
        output_schema: dict,
    ) -> None:
        # The logical output schema is session-stable. Actual provider wire
        # formats are recorded separately by write_native_request.
        self._emit(
            "session_start",
            provider=provider,
            model=model,
            prompt_hash=prompt_hash,
            output_schema=output_schema,
        )

    def write_request(
        self, turn: int, messages: list[dict], *, call: UsageCall | None = None
    ) -> None:
        # Inline frames become session-relative paths: the file the
        # frame's own tool result filed, or one filed now under this turn.
        self._turn = turn
        scrubbed = wire.scrub_messages(
            messages, lambda mime, b64: self._images.put(turn, mime, b64)
        )
        extra = {"call": call} if call is not None else {}
        self._emit("request", turn=turn, messages=scrubbed, **extra)

    def write_native_request(
        self,
        turn: int,
        body: dict,
        *,
        attempt: int = 1,
        schema_id: str | None = None,
        transport: str | None = None,
        call: UsageCall | None = None,
    ) -> None:
        """Capture the actual body from the sending adapter, without headers.

        The viewer reads messages for frames and request deltas. The full body
        also contains native tools, system blocks and provider request options.
        """
        self._turn = turn
        scrubbed = wire.scrub_request(
            body,
            lambda mime, b64: self._images.put(turn, mime, b64),
        )
        self._emit(
            "request",
            turn=turn,
            messages=scrubbed.get("input", scrubbed.get("messages", [])),
            body=scrubbed,
            attempt=attempt,
            **({"schema_id": schema_id} if schema_id is not None else {}),
            **({"transport": transport} if transport is not None else {}),
            **({"call": call} if call is not None else {}),
        )

    def write_response(
        self,
        turn: int,
        raw: dict[str, Any],
        *,
        elapsed_ms: int,
        call: UsageCall | None = None,
        producer: TurnProducer = TurnProducer("physiclaw.llm"),
        presentation: Literal["model", "plugin", "playbook"] = "model",
    ) -> None:
        self._turn = turn
        self._emit(
            "response",
            turn=turn,
            elapsed_ms=elapsed_ms,
            producer=asdict(producer),
            presentation=presentation,
            raw=raw,
            **({"call": call} if call is not None else {}),
        )

    def write_micro(self, rec: MicroRecord) -> None:
        """One conductor decision call in a single record (kind "micro")
        — self-contained, so `playbooks micro` re-asks it without the
        walk. Its frames are scrubbed to session files like a turn's
        request (the same codec, so the viewer and the re-ask read them
        back the same way)."""
        # Shallow: the scrub copies what it changes, and `asdict` would
        # deep-copy every replayed message (and frame) first.
        data = {f.name: getattr(rec, f.name) for f in fields(rec)}
        data["request"] = wire.scrub_messages(
            data["request"], lambda mime, b64: self._images.put(self._turn, mime, b64)
        )
        self._emit("micro", **data)

    def close(self) -> None:
        if not self._f.closed:
            self._f.close()

    def _emit(self, kind: str, **data: Any) -> None:
        """Fail-open like the sibling sinks (`Trace._write_event`,
        the image store): a full disk or an unserializable payload
        costs the wire record, never the session."""
        obj = {"t": iso_now(), "kind": kind, **data}
        try:
            # JSON escapes retain even unpaired surrogates from model text.
            self._f.write(json.dumps(obj) + "\n")
            self._f.flush()
        except (OSError, TypeError, ValueError):
            log.warning("model.jsonl write failed", exc_info=True)
