"""The default producer owns its LLM context, retries and response policies."""

import asyncio
import enum
import logging
import time

from physiclaw.agent.engine import assemble, compact
from physiclaw.agent.engine.facts import CONTEXT_CALLS
from physiclaw.agent.engine.runspec import EngineRun
from physiclaw.agent.engine.session import Session
from physiclaw.agent.runtime.sentinel import FAIL, STUCK
from physiclaw.agent.runtime.stopping import checkpoint
from physiclaw.common.log_style import console_text
from physiclaw.common.provider_errors import (
    ProviderConfigurationError,
    ProviderError,
    ProviderTransientError,
)
from physiclaw.contract.decision import decision_json
from physiclaw.contract.dto import (
    AssistantMessage,
    FinishReason,
    Message,
    TextBlock,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from physiclaw.contract.plugin import PluginServices, ProposedTurn, SessionSetup
from physiclaw.contract.turn import ActionEvent, TurnInput, TurnMetadata
from physiclaw.provider import ChatProvider, mcp_blocks_to_content_blocks

log = logging.getLogger(__name__)
CORRECTIVE_LIMIT = 5


class _Turn(enum.Enum):
    PROCEED = enum.auto()
    RETRY = enum.auto()
    ABORT = enum.auto()


class LLMProducer:
    """Build a private conversation from this producer's messages and shared receipts."""

    def __init__(
        self,
        provider: ChatProvider,
        run: EngineRun,
        session: Session,
        messages: list[Message],
        *,
        layout_incomplete: bool = False,
    ):
        self.provider = provider
        self.run = run
        self.session = session
        self.messages = list(messages)
        self.layout_incomplete = layout_incomplete
        self._seen_sequence = 0
        self._pending: set[str] = set()
        self._corrective_streak = 0

    async def session_setup(self, _ctx: PluginServices) -> SessionSetup | None:
        return None

    async def aclose(self) -> None:
        # The host owns the shared stateless transport.
        pass

    def _ingest(self, turn: TurnInput) -> None:
        context_receipts = self.session.context_receipts.pop("physiclaw.llm", {})
        events = [
            event for event in turn.events if event.sequence > self._seen_sequence
        ]
        missing = self._pending - {event.call_id for event in events}
        if missing:
            # A rejected proposal must not leave unpaired tool calls in context.
            self.messages.pop()
            self.messages.append(
                UserMessage(
                    content="The previous proposal has no complete tool receipts. Do not assume "
                    "it executed; check the current state before retrying."
                )
            )
            self._pending.clear()
        # Compact only history the model has already seen. Fresh receipts must
        # reach a request and an executed checkpoint before details can be folded.
        # Missing receipts mean the proposal's checkpoint was not confirmed.
        if not missing and compact.collapse_old_turns(
            self.messages,
            policy=self.provider.COLLAPSE,
            collapsed_once=self.session.collapsed_once,
        ):
            self.session.collapsed_once = True
        for event in events:
            self._seen_sequence = event.sequence
            own = event.call_id in self._pending
            if not own and event.tool in CONTEXT_CALLS:
                continue
            if not own:
                arguments = decision_json(event.arguments)
                assert isinstance(arguments, dict)
                self.messages.append(
                    AssistantMessage(
                        content="",
                        finish_reason=FinishReason.TOOL_CALLS,
                        tool_calls=[ToolCall(event.call_id, event.tool, arguments)],
                    )
                )
            result = context_receipts.get(event.call_id) if own else None
            if result is None:
                result = _event_result(event)
            self.messages.append(result)
            self._pending.discard(event.call_id)
        compact.drop_stale_screens(self.messages)

    async def advance(
        self, turn: TurnInput, *, metadata: TurnMetadata
    ) -> ProposedTurn | None:
        self._ingest(turn)
        # Corrective and transport retries retain this host turn's metadata.
        for _ in range(CORRECTIVE_LIMIT + len(self.run.policies.turn_gates) + 1):
            checkpoint()
            request, imminent = self._prepare_request(metadata.turn)
            try:
                asst = await self._chat_with_retry(request, metadata)
            except ProviderConfigurationError:
                raise  # The launcher reports setup errors; a fresh session cannot fix them.
            except Exception as exc:
                self.run.tr.write(
                    {
                        "event": "provider_failed",
                        "turn": metadata.turn,
                        "error": str(exc),
                    }
                )
                self.session.sentinel_status = STUCK
                self.session.sentinel_recap = f"provider error: {exc}"
                raise
            self.messages.append(asst)
            self.session.model_turns += 1
            called = asst.tool_names()
            shape = self._enforce_shape(asst, called, metadata.turn)
            if shape is _Turn.ABORT:
                return None
            if shape is _Turn.RETRY:
                continue
            for gate in self.run.policies.turn_gates:
                gate.observe_turn(self.session, asst)
            if self._apply_turn_gates(
                asst, called, turn=metadata.turn, compaction_imminent=imminent
            ):
                continue
            self._pending = {call.id for call in asst.tool_calls}
            for gate in self.run.policies.turn_gates:
                gate.on_turn_complete()
            return ProposedTurn(asst)
        self.session.sentinel_status = STUCK
        self.session.sentinel_recap = "model corrective retries exhausted"
        return None

    async def _chat_with_retry(
        self, messages: list[Message], metadata: TurnMetadata
    ) -> AssistantMessage:
        attempts = self.run.settings.provider_retry_attempts
        backoff = self.run.settings.retry_backoff_seconds
        last_err: Exception | None = None
        self.run.rlog.write_request(
            metadata.turn, self.provider.serialize_history(messages, metadata=metadata)
        )
        for attempt in range(1, attempts + 1):
            checkpoint()
            started = time.perf_counter()
            try:
                asst = await self.provider.chat(
                    messages, self.run.tool_schemas, metadata=metadata
                )
                # Log every actual reply, including malformed or gated replies.
                # The generic host records accepted proposals in the event trace.
                self.run.rlog.write_response(
                    metadata.turn,
                    asst.raw,
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                )
                return asst
            except ProviderTransientError as exc:
                checkpoint()
                last_err = exc
                if attempt < attempts:
                    log.warning(
                        "Provider attempt %d/%d failed: %s — retrying in %.1fs",
                        attempt,
                        attempts,
                        exc,
                        backoff,
                        exc_info=True,
                        extra={
                            "event": "provider.retry",
                            "attempt": attempt,
                            "retry_in_ms": round(backoff * 1000),
                            "console_message": f"Model request failed ({attempt}/{attempts}): {console_text(exc, 140)} — retrying in {backoff:.1f}s",
                        },
                    )
                    await asyncio.sleep(backoff)
        raise ProviderError(
            f"gave up after {attempts} attempts: {last_err}"
        ) from last_err

    def _prepare_request(self, turn: int) -> tuple[list[Message], bool]:
        """Build this producer's prompt tails without mutating its history."""
        imminent = compact.collapse_pending(
            self.messages,
            policy=self.provider.COLLAPSE,
            collapsed_once=self.session.collapsed_once,
        )
        request = assemble.apply_request_tails(
            self.messages,
            self.session,
            layout_incomplete=self.layout_incomplete,
            compaction_keep=self.provider.COLLAPSE.keep if imminent else None,
        )
        self.run.tr.write(
            {"event": "request", "turn": turn, "message_count": len(request)}
        )
        log.debug("turn %d: %d messages", turn, len(request))
        return request, imminent

    def _enforce_shape(
        self, asst: AssistantMessage, called: list[str], turn: int
    ) -> _Turn:
        if asst.finish_reason == FinishReason.CONTENT_FILTER:
            log.error("The model provider blocked the response — stopping task")
            self.session.sentinel_status = FAIL
            self.session.sentinel_recap = "content filter blocked response"
            return _Turn.ABORT
        if len(called) == 2 and called.count("note") == 1:
            self._corrective_streak = 0
            return _Turn.PROCEED
        self.run.tr.write(
            {"event": "bad_turn_shape", "turn": turn, "tool_calls": called}
        )
        log.warning(
            "turn %d: bad turn shape tool_calls=%s — injecting corrective",
            turn,
            called,
            extra={
                "console_message": "Model reply has invalid tool calls — requesting a corrected reply"
            },
        )
        corrective = (
            _corrective_for_bad_shape(called)
            if called
            else (
                "Your last turn had no tool_calls. Every turn must either call tools or call "
                "end_session(status, recap) to close. Reply again as a tool call — and include "
                "note(summary=...) alongside."
            )
        )
        self._corrective_streak += 1
        if self._corrective_streak >= CORRECTIVE_LIMIT:
            log.warning(
                "%d consecutive turn-shape correctives — giving up",
                self._corrective_streak,
                extra={
                    "console_message": "The model repeatedly returned invalid tool calls — stopping task"
                },
            )
            self.session.sentinel_status = STUCK
            self.session.sentinel_recap = (
                f"{self._corrective_streak} consecutive malformed turns — "
                "model cannot hold the [note, one-other] shape"
            )
            return _Turn.ABORT
        self.messages.pop()
        self.messages.append(UserMessage(content=corrective))
        return _Turn.RETRY

    def _apply_turn_gates(
        self,
        asst: AssistantMessage,
        called: list[str],
        *,
        turn: int,
        compaction_imminent: bool,
    ) -> bool:
        for gate in self.run.policies.turn_gates:
            rejection = gate.check(
                self.session,
                asst,
                called,
                turn=turn,
                compaction_imminent=compaction_imminent,
            )
            if rejection is not None:
                log.info("turn %d: %s", turn, rejection.log_msg)
                self.run.tr.write(
                    {"event": rejection.event, "turn": turn, **rejection.extra}
                )
                self.messages.pop()
                self.messages.append(UserMessage(content=rejection.corrective))
                return True
        return False


def _event_result(event: ActionEvent) -> ToolResultMessage:
    """Render a shared execution receipt, including host diagnostics."""
    blocks = decision_json(event.result_blocks)
    assert isinstance(blocks, list)
    content = mcp_blocks_to_content_blocks(blocks)
    if event.notices:
        content = (
            "\n".join([content, *event.notices])
            if isinstance(content, str)
            else [*content, *(TextBlock(text=text) for text in event.notices)]
        )
    return ToolResultMessage(
        tool_call_id=event.call_id, content=content, is_error=event.is_error
    )


def _corrective_for_bad_shape(called: list[str]) -> str:
    """Build a turn-specific corrective for a [note, one-other] shape
    violation.

    Naming the rejected tool nudges the model to retry the same action
    on the next turn instead of switching to whatever fits the shape.
    Without this, models often interpret the generic "re-issue as
    [note, one-other]" as licence to pick a different second tool —
    silently dropping the rejected action entirely.
    """
    # Each branch tests (n_notes, len(others)) explicitly so the
    # coverage matrix is readable top-to-bottom: no implicit fallthrough,
    # no overlap. Caller filters out (0,0) and (1,1) — every other
    # non-empty shape lands in exactly one branch below.
    n_notes = called.count("note")
    others = [c for c in called if c != "note"]
    n_others = len(others)

    if n_notes == 0 and n_others == 1:
        # Most common: agent forgot note alongside its action.
        return (
            f"Your last turn called `{others[0]}` without `note`. Every "
            f"turn = exactly `[note, one-other]`. Re-issue as "
            f"`[note(summary=...), {others[0]}(...)]` with the same arguments."
        )

    if n_notes == 0 and n_others >= 2:
        return (
            f"Your last turn called {called!r} without `note` and with "
            f"too many action tools. Every turn = exactly `[note, one-other]`. "
            f"Keep `[note(summary=...), {others[0]}(...)]` for this turn; "
            f"split {others[1:]!r} into later turns."
        )

    if n_notes == 1 and n_others == 0:
        # Naming `peek()` as the default breaks the anchoring loop when
        # the model has no concrete action in mind (e.g. ambient warm-
        # start triggers): without a suggestion, models pick `note` again
        # because every other tool feels unjustified.
        return (
            "Your last turn called `note` alone with no action tool. "
            "Every turn = exactly `[note, one-other]`. If unsure what to "
            "do next, `peek()` is the safe default — re-issue as "
            "`[note(summary=...), peek()]`."
        )

    if n_notes == 1 and n_others >= 2:
        return (
            f"Your last turn called {called!r} — `note` plus {n_others} "
            f"action tools. Every turn = exactly `[note, one-other]`. "
            f"Keep `[note(summary=...), {others[0]}(...)]` for this turn; "
            f"split {others[1:]!r} into later turns."
        )

    if n_notes >= 2:
        return (
            f"Your last turn called `note` {n_notes} times. Every turn = "
            f"exactly `[note, one-other]`. Re-issue with `note(summary=...)` "
            f"once plus one action tool."
        )

    raise AssertionError(f"unreachable: called={called!r}")
