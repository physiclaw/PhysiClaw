"""The default producer owns its LLM context, retries and response policies."""

import asyncio
import logging
import random
import time
from dataclasses import replace

from physiclaw.agent.engine import assemble, compact
from physiclaw.agent.engine.facts import CONTEXT_CALLS
from physiclaw.agent.engine.output import ActionCatalog
from physiclaw.agent.engine.runspec import EngineRun
from physiclaw.agent.engine.session import Session
from physiclaw.agent.llm.context_policy import DEFAULT_CONTEXT_POLICY, ContextPolicy
from physiclaw.agent.runtime.sentinel import FAIL, STUCK
from physiclaw.agent.runtime.stopping import checkpoint
from physiclaw.common.log_style import console_text
from physiclaw.common.provider_errors import (
    ProviderConfigurationError,
    ProviderError,
    ProviderPermanentError,
    ProviderTransientError,
)
from physiclaw.contract.chat import ChatProvider
from physiclaw.contract.content import mcp_blocks_to_content_blocks
from physiclaw.contract.decision import decision_json
from physiclaw.contract.dto import (
    AssistantMessage,
    ContentBlock,
    Message,
    OutputResultMessage,
    TextBlock,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from physiclaw.contract.plugin import PluginServices, ProposedTurn, SessionSetup
from physiclaw.contract.structured import StructuredCompletion
from physiclaw.contract.turn import ActionEvent, TurnInput, TurnMetadata, TurnOutput

log = logging.getLogger(__name__)
CORRECTIVE_LIMIT = 5


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
        catalog: ActionCatalog,
        context_policy: ContextPolicy = DEFAULT_CONTEXT_POLICY,
    ):
        self.provider = provider
        self.context_policy = context_policy
        self.run = run
        self.session = session
        self.messages = list(messages)
        self.layout_incomplete = layout_incomplete
        self._seen_sequence = 0
        self._corrective_streak = 0
        self.catalog = catalog
        self._output_spec = catalog.spec()
        provider.validate_output_spec(self._output_spec)
        self._schema_id = catalog.identity
        self._pending_reply: AssistantMessage | None = None
        self._pending_calls: list[ToolCall] = []

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
        by_id = {event.call_id: event for event in events}
        pending_ids = {call.id for call in self._pending_calls}
        missing = pending_ids - by_id.keys()
        if not missing and compact.collapse_old_turns(
            self.messages,
            policy=self.context_policy.collapse,
            collapsed_once=self.session.collapsed_once,
        ):
            self.session.collapsed_once = True
        if self._pending_reply is not None:
            context_results = []
            content: list[ContentBlock] = []
            confirmed_calls = []
            for call in self._pending_calls:
                event = by_id.get(call.id)
                if event is None:
                    result = ToolResultMessage(
                        call.id,
                        "Receipt unavailable. Outcome unknown; do not repeat automatically. Inspect the current state first.",
                        is_error=True,
                    )
                else:
                    result = context_receipts.get(call.id) or _event_result(event)
                    if event.executed and not event.is_error:
                        confirmed_calls.append(call)
                # Context state is a separate outcome from the selected action.
                label = "scratchpad/summary" if call.name == "note" else call.name
                status = "error" if result.is_error else "ok"
                content.append(TextBlock(text=f"{label}: {status}"))
                if event is not None:
                    content.append(TextBlock(text=f"executed: {event.executed}"))
                content.extend(
                    [TextBlock(text=result.content)]
                    if isinstance(result.content, str)
                    else result.content
                )
                if call.name == "Skill" and not result.is_error:
                    content.append(
                        TextBlock(
                            text="Apply this procedure through TurnOutput.action, one action per turn; keep summary and scratchpad as top-level fields."
                        )
                    )
                # Only durable text artifacts need a compaction copy. Never retain
                # images here: stale-screen projection must not restore old pixels.
                if call.name in compact.MEMORY_TOOL_NAMES | compact.SKILL_TOOL_NAMES:
                    context_results.append(result)
            reply = replace(self._pending_reply, context_calls=confirmed_calls)
            self.messages[-1] = reply
            self.messages.append(
                OutputResultMessage(
                    reply,
                    content,
                    is_error=bool(missing)
                    or any(
                        by_id[c.id].is_error
                        for c in self._pending_calls
                        if c.id in by_id
                    ),
                    context_results=context_results,
                )
            )
        for event in events:
            self._seen_sequence = event.sequence
            if event.call_id in pending_ids or event.tool in CONTEXT_CALLS:
                continue
            result = _event_result(event)
            content = [
                TextBlock(
                    text=f"Observed another producer's {event.tool}; executed={event.executed}; error={event.is_error}."
                )
            ]
            content.extend(
                [TextBlock(text=result.content)]
                if isinstance(result.content, str)
                else result.content
            )
            self.messages.append(UserMessage(content=content))
        self._pending_calls.clear()
        self._pending_reply = None
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
                completion = await self._chat_with_retry(request, metadata)
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
                permanent = isinstance(exc, ProviderPermanentError)
                self.session.sentinel_status = FAIL if permanent else STUCK
                self.session.sentinel_recap = f"provider error: {exc}"
                if permanent:
                    return None
                raise
            self.session.model_turns += 1
            if completion.failure is not None:
                failure = completion.failure
                self.run.tr.write(
                    {
                        "event": "bad_turn_shape",
                        "turn": metadata.turn,
                        "failure_kind": failure.kind,
                        "error": failure.message,
                    }
                )
                if failure.kind in ("refusal", "truncation"):
                    self.session.sentinel_status = FAIL
                    self.session.sentinel_recap = failure.message
                    if failure.kind == "truncation":
                        self.session.sentinel_recap += (
                            "; request shorter output or increase engine.provider_max_output_tokens "
                            "within the model's limit (includes reasoning tokens)"
                        )
                    return None
                self._corrective_streak += 1
                if self._corrective_streak >= CORRECTIVE_LIMIT:
                    break
                # Do not append a rejected reply: removing the complete exchange
                # avoids replaying malformed calls or signed partial blocks.
                self.messages.append(
                    UserMessage(
                        content=f"TurnOutput rejected before execution: {failure.message}. Submit one corrected TurnOutput with summary, scratchpad and exactly one eligible action."
                    )
                )
                continue
            assert completion.value is not None
            asst = self.catalog.lower(completion.value)
            # Trace/accounting keep the provider reply. Execution uses only fresh
            # internal IDs; opaque provider metadata stays on the original history.
            asst.raw = completion.reply.raw
            asst.usage = completion.reply.usage
            asst.response_id = completion.reply.response_id
            asst.response_model = completion.reply.response_model
            self.messages.append(completion.reply)
            self._corrective_streak = 0
            called = asst.tool_names()
            for gate in self.run.policies.turn_gates:
                gate.observe_turn(self.session, asst)
            if self._apply_turn_gates(
                asst, called, turn=metadata.turn, compaction_imminent=imminent
            ):
                continue
            self._pending_reply = completion.reply
            self._pending_calls = list(asst.tool_calls)
            for gate in self.run.policies.turn_gates:
                gate.on_turn_complete()
            return ProposedTurn(asst)
        self.session.sentinel_status = STUCK
        self.session.sentinel_recap = "model corrective retries exhausted"
        return None

    async def _chat_with_retry(
        self, messages: list[Message], metadata: TurnMetadata
    ) -> StructuredCompletion[TurnOutput]:
        attempts = self.run.settings.provider_retry_attempts
        backoff = self.run.settings.retry_backoff_seconds
        last_err: Exception | None = None
        for attempt in range(1, attempts + 1):
            checkpoint()
            if self.run.deadline is not None and time.monotonic() >= self.run.deadline:
                self.session.budget_exhausted = True
                raise ProviderError("Provider deadline expired")
            started = time.perf_counter()
            try:
                result = await self.provider.generate_structured(
                    messages,
                    self._output_spec,
                    metadata=metadata,
                    on_request=lambda body: self.run.rlog.write_native_request(
                        metadata.turn,
                        body,
                        attempt=attempt,  # noqa: B023 -- provider invokes it within this awaited call.
                        schema_id=self._schema_id,
                        transport=self.provider.structured_transport,
                    ),
                )
                asst = result.reply
                # Log every actual reply, including malformed or gated replies.
                # The generic host records accepted proposals in the event trace.
                self.run.rlog.write_response(
                    metadata.turn,
                    asst.raw,
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                )
                return result
            except ProviderTransientError as exc:
                checkpoint()
                last_err = exc
                failed_reply = getattr(exc, "reply", None)
                if failed_reply is not None:
                    self.run.rlog.write_response(
                        metadata.turn,
                        failed_reply.raw,
                        elapsed_ms=int((time.perf_counter() - started) * 1000),
                    )
                if attempt < attempts:
                    delay = retry_delay(exc, attempt, backoff)
                    if (
                        self.run.deadline is not None
                        and time.monotonic() + delay >= self.run.deadline
                    ):
                        self.session.budget_exhausted = True
                        raise ProviderError(
                            "Provider retry would exceed the session deadline"
                        ) from exc
                    log.warning(
                        "Provider attempt %d/%d failed: %s — retrying in %.1fs",
                        attempt,
                        attempts,
                        exc,
                        delay,
                        exc_info=True,
                        extra={
                            "event": "provider.retry",
                            "attempt": attempt,
                            "retry_in_ms": round(delay * 1000),
                            "console_message": f"Model request failed ({attempt}/{attempts}): {console_text(exc, 140)} — retrying in {delay:.1f}s",
                        },
                    )
                    await asyncio.sleep(delay)
        raise ProviderError(
            f"gave up after {attempts} attempts: {last_err}"
        ) from last_err

    def _prepare_request(self, turn: int) -> tuple[list[Message], bool]:
        """Build this producer's prompt tails without mutating its history."""
        imminent = compact.collapse_pending(
            self.messages,
            policy=self.context_policy.collapse,
            collapsed_once=self.session.collapsed_once,
        )
        request = assemble.apply_request_tails(
            self.messages,
            self.session,
            layout_incomplete=self.layout_incomplete,
            compaction_keep=self.context_policy.collapse.keep if imminent else None,
        )
        self.run.tr.write(
            {"event": "request", "turn": turn, "message_count": len(request)}
        )
        log.debug("turn %d: %d messages", turn, len(request))
        return request, imminent

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


def retry_delay(error: ProviderTransientError, attempt: int, base: float) -> float:
    """Cap local backoff at 60s; a server's Retry-After is a minimum wait."""
    delay = min(60.0, base * 2 ** min(attempt - 1, 16))
    delay = min(60.0, delay + random.uniform(0, delay * 0.1))
    return max(delay, error.retry_after or 0.0)
