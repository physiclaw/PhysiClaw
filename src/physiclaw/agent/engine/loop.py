"""Generic turn arbitration and execution, with no producer-owned context.

Every slot receives one immutable runtime snapshot and turn metadata. The
first valid proposal wins; tool validation, permissions, execution and the
session budget use the same path for every producer.
"""

import asyncio
import logging
import time
from dataclasses import asdict, dataclass

from physiclaw.agent.engine import memory, trajectory
from physiclaw.agent.engine.dispatch import dispatch
from physiclaw.agent.engine.provenance import ProducedTurn
from physiclaw.agent.engine.runspec import EngineRun
from physiclaw.agent.engine.session import Session
from physiclaw.agent.engine.validator import validate_arguments
from physiclaw.agent.runtime.sentinel import STUCK
from physiclaw.agent.runtime.stopping import checkpoint
from physiclaw.agent.trace import Trace
from physiclaw.common import daylog
from physiclaw.common.bbox import parse_box
from physiclaw.common.gesture_vocab import PRESS_TOOLS, RUN_MACRO
from physiclaw.common.logger import (
    PLAYBOOK_ACCENT,
    bind_log_context,
    reset_log_context,
    set_tag_accent,
)
from physiclaw.contract.decision import decision_json
from physiclaw.contract.dto import (
    AssistantMessage,
    FinishReason,
    ToolCall,
)
from physiclaw.contract.plugin import ProposedTurn
from physiclaw.contract.turn import TurnInput, TurnMetadata

log = logging.getLogger(__name__)


@dataclass
class _LoopState:
    turn: int = 0


async def drive(run: EngineRun, session: Session) -> None:
    """Run turns within the time budget, including awaited calls and retries.
    Whatever ends the loop, clear the playbook's terminal colour."""
    state = _LoopState()
    remaining = (
        max(0.0, run.deadline - time.monotonic()) if run.deadline is not None else None
    )
    budget = asyncio.timeout(remaining)
    context = bind_log_context()
    try:
        async with budget:
            await _drive(run, session, state)
    except TimeoutError:
        if not budget.expired():
            raise  # An unrelated timeout must retain its own failure path.
        _close_budget_exhausted(run, session, state.turn)
    finally:
        set_tag_accent(None)
        reset_log_context(context)


async def _drive(run: EngineRun, session: Session, state: _LoopState) -> None:
    """Ask producers in order; validate and dispatch the first accepted turn."""
    for turn in range(run.settings.max_turns):
        # drive() owns the enclosing context scope and restores it on every
        # exit. Each provider/plugin call inherits its turn, including retries.
        bind_log_context(turn=turn)
        state.turn = turn
        if _stop_if_expired(run, session, turn):
            return
        run.tr.write({"event": "turn_started", "turn": turn})
        session.plan.tick_turn()
        session.guard.observe_step(session.plan.current_step())
        produced = await _call_producer(run, session, turn)
        if _stop_if_expired(run, session, turn):
            return
        if produced is None:
            return  # No accepted proposal; the session outcome is already set.
        asst = produced.message

        session.active_plugin = produced.binding
        # The terminal's tag wears the driver's colour from here until
        # the other side produces a turn — set where the session's own
        # record of the producer is set, so the two cannot disagree.
        set_tag_accent(PLAYBOOK_ACCENT if produced.presentation == "playbook" else None)
        try:
            successful_calls = await _dispatch_turn(run, session, asst, turn)
        finally:
            session.active_plugin = None
        if session.budget_exhausted:
            return
        if session.sentinel_status is None and _stop_if_expired(run, session, turn):
            return
        _finalize_turn(session, successful_calls, turn)

        if session.sentinel_status:
            return
    else:
        log.warning("engine hit max turns (%d)", run.settings.max_turns)
        session.sentinel_status = STUCK
        session.sentinel_recap = f"max turns ({run.settings.max_turns}) reached"


def _stop_if_expired(run: EngineRun, session: Session, turn: int) -> bool:
    checkpoint()
    # Calls that finish without yielding can cross the deadline before the
    # asyncio timer gets a chance to cancel us. Do not start another action.
    if run.deadline is not None and time.monotonic() >= run.deadline:
        _close_budget_exhausted(run, session, turn)
        return True
    return False


def _close_budget_exhausted(run: EngineRun, session: Session, turn: int) -> None:
    """Close the session STUCK when the wall-clock budget runs out — the
    time counterpart of the max-turns backstop. Writes the mid-task
    recovery breadcrumb first (the guard needs the sentinel still unset),
    and flags `budget_exhausted` so the outcome is not retried: a slow
    environment would burn every retry the same way."""
    budget = run.settings.max_session_seconds
    log.warning(
        "session wall-clock budget (%ds) exhausted at turn %d — closing STUCK",
        budget,
        turn,
    )
    run.tr.write({"event": "budget_exhausted", "turn": turn, "budget_seconds": budget})
    log_external_stop(session, run.tr, cause="wall-clock budget exhausted", event=None)
    session.budget_exhausted = True
    session.sentinel_status = STUCK
    session.sentinel_recap = f"wall-clock budget ({budget}s) exhausted"


def log_external_stop(
    session: Session,
    tr: Trace | None,
    *,
    cause: str = "stopped externally",
    event: str | None = "stopped_externally",
) -> None:
    """Deterministic mid-task checkpoint when a session is cut short —
    external stop (Ctrl-C, runtime shutdown) or the wall-clock budget.
    The model gets no final turn, so the harness writes the daily-log
    line the doctrine would have required — the auto-injected recovery
    breadcrumb for the next wake. Sync file I/O only (this runs on the
    cancellation path); best effort, never raises."""
    if session.sentinel_status is not None or not session.plan.is_drafted():
        return  # closed cleanly, or nothing recoverable yet
    try:
        step = session.plan.current_step() or "(none in_progress)"
        done, total = session.plan.progress()
        memory.append_log(
            daylog.stamped(
                f"engine: {cause} mid-task ({done}/{total} steps done) — "
                f"in-progress: {step}. Verify app state before resuming."
            )
        )
        if tr is not None and event:
            tr.write({"event": event})
        log.info("%s mid-task — recovery line logged", cause)
    except Exception:
        log.exception("mid-task recovery log failed")


# ---------- per-turn phases ----------


async def _dispatch_turn(
    run: EngineRun,
    session: Session,
    asst: AssistantMessage,
    turn: int,
) -> list[ToolCall]:
    """Run the turn's tool_calls — principle 6: exactly one ToolResult per
    ToolCall, in order, in the very next messages. Flags a length-truncation
    warning first: if finish=length the final tool_call's arguments may be cut
    off — the validator catches it and pairs an error result, same path as any
    bad args. Returns the calls that succeeded for trajectory bookkeeping."""
    if asst.finish_reason == FinishReason.LENGTH:
        log.warning(
            "turn %d: finish=length; last tool_call args may be truncated",
            turn,
            extra={
                "console_message": "Model response reached its length limit — checking tool arguments"
            },
        )
        run.tr.write({"event": "finish_length_warning", "turn": turn})

    successful_calls: list[ToolCall] = []
    for call in asst.tool_calls:
        if _stop_if_expired(run, session, turn):
            return successful_calls
        context = bind_log_context(tool=call.name, tool_call_id=call.id)
        try:
            result = await dispatch(run, session, call, turn)
        finally:
            reset_log_context(context)
        if not result.is_error:
            successful_calls.append(call)
    return successful_calls


def _finalize_turn(
    session: Session, successful_calls: list[ToolCall], turn: int
) -> None:
    """Record successful shared-state writes, independent of their producer."""
    trajectory.record(
        session,
        turn,
        plan_updated=any(call.name == "update_progress" for call in successful_calls),
        scratchpad_written=any(
            call.name == "note" and call.arguments.get("scratchpad") is not None
            for call in successful_calls
        ),
    )


# ---------- helpers ----------


def _validate_turn(turn: AssistantMessage) -> None:
    """Reject unusable plugin output before it can suppress the fallback.

    This checks transcript structure, without imposing LLM note discipline.
    Tool existence, argument schemas and action guards remain dispatch's job.
    """
    if not isinstance(turn, AssistantMessage):
        raise TypeError("proposal must contain an AssistantMessage")
    if not turn.tool_calls or turn.finish_reason in (
        FinishReason.CONTENT_FILTER,
        FinishReason.ERROR,
    ):
        raise ValueError("producer returned no executable turn")
    ids: set[str] = set()
    for call in turn.tool_calls:
        if (
            not isinstance(call, ToolCall)
            or not isinstance(call.id, str)
            or not call.id.strip()
            or call.id in ids
            or not isinstance(call.name, str)
            or not call.name.strip()
            or not isinstance(call.arguments, dict)
        ):
            raise ValueError("producer returned invalid tool calls or duplicate IDs")
        ids.add(call.id)


def _proposed_message(
    proposal: ProposedTurn, facts: TurnInput, *, private_macros: frozenset[str]
) -> AssistantMessage:
    """Check an optional observation binding and its targets before accepting."""
    if not isinstance(proposal, ProposedTurn):
        raise TypeError("producer must return ProposedTurn or None")
    message = proposal.message
    _validate_turn(message)
    observation = facts.observation
    if proposal.observation_id is not None or proposal.targets:
        if observation is None or proposal.observation_id != observation.id:
            raise ValueError("proposal observation is absent or stale")
    if proposal.targets:
        assert (
            observation is not None
        )  # Checked together with the observation ID above.
        elements = {
            observation.element_ref(element): element
            for element in observation.elements
        }
        calls = {call.id: call for call in message.tool_calls}
        for call_id, ref in proposal.targets.items():
            if call_id not in calls or ref not in elements:
                raise ValueError("proposal target is not in its observation or calls")
            call = calls[call_id]
            if (
                call.name in PRESS_TOOLS
                and parse_box(call.arguments.get("bbox")) != elements[ref].bbox
            ):
                raise ValueError("proposal bbox differs from its observed target")
    # Bound actions validate before arbitration; ordinary tool turns still use
    # dispatch's schema/permission checks, with their actual refusal results.
    if proposal.observation_id is not None:
        for call in message.tool_calls:
            if call.name not in facts.tools:
                raise ValueError("proposal names an unavailable tool")
            schema = decision_json(facts.tools[call.name])
            assert isinstance(schema, dict)
            validate_arguments(call.arguments, schema)
            if call.name == RUN_MACRO:
                name = call.arguments.get("name", "").strip()
                if "/" in name and name not in private_macros:
                    raise ValueError(
                        "proposal cannot use another plugin's private macro"
                    )
    return message


async def _advance(
    run: EngineRun, session: Session, metadata: TurnMetadata
) -> ProducedTurn | None:
    """Every slot receives the same immutable input and metadata; first accepted wins."""
    runtime_input = session.facts.snapshot(
        schemas=run.schema_by_name, progress=session.plan.runtime_snapshot()
    )
    for binding in run.plugins:
        checkpoint()
        started = time.perf_counter()
        try:
            proposal = await binding.plugin.advance(runtime_input, metadata=metadata)
            if proposal is not None:
                message = _proposed_message(
                    proposal, runtime_input, private_macros=binding.private_macros
                )
                return ProducedTurn(
                    message, binding, int((time.perf_counter() - started) * 1000)
                )
        except Exception:
            log.exception("producer %s failed — trying the next slot", binding.id)
        if session.sentinel_status is not None:
            return None
    return None


async def _call_producer(
    run: EngineRun, session: Session, turn: int
) -> ProducedTurn | None:
    metadata = TurnMetadata(
        session_id=session.session_id,
        turn=turn,
        deadline=run.deadline,
        turns_remaining=run.settings.max_turns - turn,
    )
    started = time.perf_counter()
    produced = await _advance(run, session, metadata)
    if produced is None:
        if session.sentinel_status is None:
            session.sentinel_status = STUCK
            session.sentinel_recap = "no producer supplied an executable turn"
        return None
    asst = produced.message
    run.tr.write(
        {
            "event": "response",
            "turn": turn,
            "finish_reason": asst.finish_reason,
            "content_len": len(asst.content or ""),
            "producer": asdict(produced.producer),
            "presentation": produced.presentation,
            "arbitration_elapsed_ms": int((time.perf_counter() - started) * 1000),
            "response_id": asst.response_id,
            "response_model": asst.response_model,
            "elapsed_ms": produced.elapsed_ms,
            "tool_calls": [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                for tc in asst.tool_calls
            ],
        }
    )
    calls = ", ".join(asst.tool_names())
    log.info(
        "turn %d: %s → %s",
        turn,
        produced.binding.name,
        calls,
        extra={
            "event": "turn.planned",
            "turn": turn,
            "duration_ms": produced.elapsed_ms,
            "console_message": f"Turn {turn} · {produced.binding.name}: {calls}",
        },
    )
    return produced
