"""Micro-calls — the conductor's scoped model calls, one channel.

Two live in an agent step: `agent_fields` (a pure-text call: the
author's prompt in, declared fields out) and `agent_act` (one episode
turn: a tool call — tap, scroll, back, run_macro, done, or escalate,
each with its own args). Three more are the session's thread
(`thread.py`), which asks about the errand rather than a screen:
`parse_task` (does the chat thread assign a task a playbook covers?),
`read_reply` (a reply the ask's own yes/no words could not decide) and
`summarize` (the record a completed walk closes on). Each call's shape
is ONE row of `calltable.SPECS`; the shapes are `decision.py`, the
reply reader `answers.py`.

`MicroCaller` is the channel: one provider call with one transient
retry, one repair retry on a reply the call's own rules refuse, a
confidence judged against the config floor, and every round-trip in
the trace and the wire log. Anything else resolves to no outcome and
the walk hands over — escalation, never a guess. Wired by `plugin.py`
off the setup context.
"""

import asyncio
import logging
import time
from typing import Any, Callable

from physiclaw.common.config import CONFIG
from physiclaw.common.log_style import console_text
from physiclaw.common.provider_errors import ProviderTransientError
from physiclaw.conductor.micro.answers import parse_reply
from physiclaw.conductor.micro.calltable import SPECS
from physiclaw.conductor.micro.compose import messages
from physiclaw.conductor.micro.decision import (
    DecisionRequest,
    Macro,
    MicroOutcome,
    MicroResult,
    Tap,
)
from physiclaw.conductor.micro.moves import reply_args
from physiclaw.contract.dto import (
    USAGE_CALL_MICRO,
    AssistantMessage,
    ContentBlock,
    Message,
    MicroRecord,
    UserMessage,
    role_of,
)
from physiclaw.contract.plugin import EventSink, SessionChatProvider, WireSink
from physiclaw.contract.turn import TurnMetadata
from physiclaw.provider import ChatProvider
from physiclaw.provider.chat.wire import (
    anthropic_image_part,
    encode_content,
    turn_metadata_message,
)

log = logging.getLogger(__name__)


class MicroCaller:
    """Scoped model calls for live sessions and rehearsals.

    The constructor borrows its provider; `owning` takes responsibility
    for closing a standalone provider. Both use the same validation,
    confidence floor, and diagnostic sinks.
    """

    def __init__(
        self,
        # The fail-open floor only ever answers `chat` — the structural
        # contract slice, so the seam can hand us the session provider
        # without the conductor demanding the full ChatProvider surface.
        provider: SessionChatProvider,
        *,
        confidence_floor: float,
        tr: "EventSink | None" = None,
        rlog: "WireSink | None" = None,
        owned_factory: "Callable[[], ChatProvider] | None" = None,
    ):
        self._provider = provider
        self._floor = confidence_floor
        self._tr = tr
        self._rlog = rlog
        # Live sessions build an optional micro provider on the first
        # call. Rehearsals set `_owned` directly through `owning`.
        # Only `_owned` is ours to close; the session provider is borrowed.
        self._owned_factory = owned_factory
        self._owned: ChatProvider | None = None

    @classmethod
    def owning(
        cls,
        provider: ChatProvider,
        *,
        confidence_floor: float,
        rlog: WireSink | None = None,
    ) -> "MicroCaller":
        """Take ownership of a standalone provider, as rehearsals do.

        The normal constructor borrows the engine's session provider;
        this caller instead closes the supplied provider at teardown,
        even when no decision was made.
        """
        caller = cls(provider, confidence_floor=confidence_floor, rlog=rlog)
        caller._owned = provider
        return caller

    def _live_provider(self) -> SessionChatProvider:
        if self._owned_factory is not None:
            factory, self._owned_factory = self._owned_factory, None
            try:
                self._owned = factory()
            except Exception:
                # One attempt; a broken cheap tier falls back to the
                # session model permanently (fail-open, logged once).
                log.warning(
                    "micro provider unusable — using the session model",
                    exc_info=True,
                )
        return self._owned if self._owned is not None else self._provider

    async def aclose(self) -> None:
        """Close an owned provider once; never close a borrowed one."""
        owned, self._owned = self._owned, None
        if owned is not None:
            await owned.aclose()

    async def run(
        self, req: DecisionRequest, *, metadata: TurnMetadata | None = None
    ) -> MicroResult:
        """One decision. `result.outcome` is None when the caller should
        escalate. Never raises."""
        t0 = time.perf_counter()
        try:
            outcome, detail, attempts = await self._ask(req, metadata)
        except Exception as e:
            _log_provider_failure(req, e)
            outcome, detail, attempts = None, "provider error", 0
        result = MicroResult(
            outcome=outcome,
            detail=detail,
            attempts=attempts,
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
        )
        try:
            self._trace(req, result)
        except Exception:
            log.warning("micro decision event write failed", exc_info=True)
        return result

    async def _ask(
        self, req: DecisionRequest, metadata: TurnMetadata | None
    ) -> "tuple[MicroOutcome | None, str, int]":
        """One decision on the live provider: fresh messages, one repair
        retry, floor judgment. No outcome means escalate — the walk hands
        over rather than asking a second model the same question."""
        allowed = SPECS[req.call].answer_space(req)
        provider = self._live_provider()
        msgs = messages(req)
        attempts = 0
        err = ""
        for attempts in (1, 2):  # one bounded repair retry
            try:
                asst = await _chat(provider, msgs, req, metadata)
            except Exception as e:
                # Escalate HERE, not via run()'s catch-all, so the
                # attempt count the trace records stays exact. (Tokens are
                # the provider's `usage` events, one per attempt.)
                _log_provider_failure(req, e)
                return None, "provider error", attempts
            parsed, err = parse_reply(asst.content or "", allowed, req.call)
            try:
                self._log_wire(req, msgs, asst, allowed, parsed, metadata)
            except Exception:
                log.warning("micro wire record write failed", exc_info=True)
            if parsed is None:
                log.info(
                    "micro %s (%s) attempt %d invalid: %s",
                    req.call,
                    req.node_id,
                    attempts,
                    err,
                    extra={
                        "event": "provider.reply_invalid",
                        "console": attempts == 1,
                        "console_message": "Decision reply invalid — retrying once",
                    },
                )
                # Repair retry: the model sees its own reply + the exact
                # validation error, once. A second miss escalates.
                msgs = [
                    *msgs,
                    asst,
                    UserMessage(
                        content=f"Invalid: {err}. Reply with ONLY the JSON object."
                    ),
                ]
                continue
            answer, reason, confidence, obj = parsed
            if confidence < self._floor:
                log.info(
                    "micro %s (%s): confidence %.2f below floor %.2f — escalating",
                    req.call,
                    req.node_id,
                    confidence,
                    self._floor,
                )
                return None, f"confidence {confidence:.2f} below floor", attempts
            outcome = SPECS[req.call].to_outcome(req, answer, reason, confidence, obj)
            return outcome, reason, attempts
        return None, f"invalid after repair retry: {err}", attempts

    def _log_wire(
        self,
        req: DecisionRequest,
        messages: list[Message],
        asst: AssistantMessage,
        allowed: tuple[str, ...],
        parsed: "tuple[str, str, float, dict[str, Any]] | None",
        metadata: TurnMetadata | None,
    ) -> None:
        """One round-trip to the wire sink, whole — every message, an
        episode's replayed history included (its replayed assistant
        turns are the contract's re-serialisation, not the raw replies,
        so a trimmed record could not be rebuilt byte for byte). Frames
        ride as typed blocks the sink scrubs to session files, the way
        a turn's request is kept. The prefix repeats per call, bounded
        by the call limit: a long episode over dense screens logs a few
        megabytes of text."""
        if self._rlog is None:
            return
        if metadata is not None:
            messages = [*messages, turn_metadata_message(metadata)]
        self._rlog.write_micro(
            MicroRecord(
                call=req.call,
                node=req.node_id,
                thinking=req.thinking,
                allowed=allowed,
                answer=parsed[0] if parsed else None,
                confidence=parsed[2] if parsed else None,
                request=[
                    {"role": role_of(m), "content": wire_content(m.content)}
                    for m in messages
                ],
                raw=asst.raw,
                reason=parsed[1] if parsed else None,
                args=reply_args(parsed[3]) if parsed else None,
            )
        )

    def _trace(self, req: DecisionRequest, result: MicroResult) -> None:
        """The decision event — one per decision, whatever happened; the
        trace renders it once and mirrors that line into the process
        log. Its tokens are the provider's `usage` event (written by the
        provider itself, under the model that answered — the cheap tier
        when one is wired)."""
        if self._tr is None:
            return
        self._tr.write(
            {
                "event": "micro_call",
                "call": req.call,
                "node": req.node_id,
                # An episode turn's listed elements — the count says how
                # much screen the decision saw (a truncated one is
                # visible here, not only in the process log) — and
                # whether the frame rode beside them.
                "rows": len(req.elements),
                "frame": req.frame is not None,
                "out": result.outcome.out if result.outcome else None,
                # What an action touched, as the model spoke it.
                "label": _picked_words(result.outcome),
                "confidence": (result.outcome.confidence if result.outcome else None),
                "detail": result.detail,
                "attempts": result.attempts,
                "elapsed_ms": result.elapsed_ms,
            }
        )


def _log_provider_failure(req: DecisionRequest, error: Exception) -> None:
    log.warning(
        "micro %s (%s): provider failed — %s",
        req.call,
        req.node_id,
        error,
        exc_info=(type(error), error, error.__traceback__),
        extra={"console_message": "Decision request failed: " + console_text(error)},
        stacklevel=2,
    )


def _picked_words(outcome: MicroOutcome | None) -> str | None:
    picked = outcome.picked if outcome else None
    if isinstance(picked, Tap):
        return picked.label
    if isinstance(picked, Macro):
        return picked.name
    return None


def wire_content(content: "str | list[ContentBlock] | Any") -> Any:
    """A message's content in the wire record's shape: text as is, a
    block list as typed dicts — the provider codec's dispatch with the
    base64 `image` part the wire scrubber reads back
    (`contract.wire.scrub_block` / `image_ref`), so a micro record's
    frames are filed and shown exactly like a turn's."""
    return encode_content(
        content, image_part=anthropic_image_part, empty="", label="micro"
    )


async def _chat(
    provider: SessionChatProvider,
    messages: list[Message],
    req: DecisionRequest,
    metadata: TurnMetadata | None,
) -> AssistantMessage:
    """One provider call with ONE transient retry (the providers' own
    taxonomy: timeout / 429 / 5xx — permanent 4xx and real bugs fail
    fast): a blip on the cheap tier is common and permanent escalation
    is too big a price for it. Raises whatever the second try raises."""
    # How much the model may think is the step's `think:` (the vendor
    # translates it, see `BaseChatProvider.thinking_params`); the reply's
    # `reason` field is the chain of thought the contract always gets.
    try:
        return await provider.chat(
            messages,
            [],
            metadata=metadata,
            purpose=USAGE_CALL_MICRO,
            thinking=req.thinking,
        )
    except ProviderTransientError:
        log.info(
            "micro %s (%s): transient provider error — one retry",
            req.call,
            req.node_id,
            exc_info=True,
            extra={
                "event": "provider.retry",
                "console": True,
                "console_message": "Decision request interrupted — retrying once",
            },
        )
        await asyncio.sleep(CONFIG.engine.retry_backoff_seconds)
        return await provider.chat(
            messages,
            [],
            metadata=metadata,
            purpose=USAGE_CALL_MICRO,
            thinking=req.thinking,
        )
