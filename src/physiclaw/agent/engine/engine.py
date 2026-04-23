"""Engine — native tool-call loop. Follows OpenClaw's 7 principles.

  1. Structure via provider API: tools=[...] + message.tool_calls, not JSON.
  2. Normalized at the boundary: Provider returns AssistantMessage; raw
     provider-specific fields (reasoning_content) stripped before echo.
  3. Real finish_reason preserved and routed (length / content_filter / stop).
  4. Arguments validated against JSONSchema before dispatch.
  5. Errors marked: failed tool_calls get synthetic tool_result(is_error=True);
     finish_reason="error" / "length" / "content_filter" drive recovery.
  6. Transcript stays API-legal: each ToolCall gets exactly one ToolResult
     with matching tool_call_id in the very next message.
  7. Loop is the driver: model → tool_calls → dispatch → tool_results → model.
"""
import asyncio
import datetime as dt
import logging
import time
from typing import Any

from physiclaw.agent.engine import builtin_tool, compact, jobs, memory, plan, prompt, skill
from physiclaw.agent.engine.builtin_tool import LocalTool, Session
from physiclaw.agent.engine.mcp_tool import McpClient, get_mcp, list_tools_cached
from physiclaw.agent.engine.dto import AssistantMessage, FinishReason, ToolCall, ToolResult
from physiclaw.agent.engine.provider import (
    Provider,
    ProviderTransientError,
    assistant_to_wire,
    blocks_to_tool_content,
    make_provider,
    tool_result_to_wire,
)
from physiclaw.agent.engine.trace import RawLog, Trace, brief, brief_args, brief_content
from physiclaw.agent.engine.validator import ValidationError, validate_arguments
from physiclaw.agent.runtime.hook import Trigger
from physiclaw.agent.runtime.sentinel import FAIL, STUCK, WAIT

log = logging.getLogger(__name__)

# Runaway-loop backstop, not a context-safety limit. Prompt tokens grow
# ~624·t + 13k empirically (R²=0.97); at 1M context (Qwen3.6-plus) the
# hard wall is ~1,580 turns, so 200 leaves ample headroom.
MAX_TURNS = 200
MAX_ATTEMPTS = 3
RETRY_BACKOFF = 5.0
WAIT_DEFAULT_MINUTES = 15


async def run(
    triggers: list[Trigger], *, provider_name: str
) -> None:
    """Run an engine session for `triggers`, retrying on STUCK.

    STUCK happens when the loop hit MAX_TURNS without a clean close, the
    provider exhausted its retries, or the session crashed. Up to
    MAX_ATTEMPTS fresh attempts run before we accept the STUCK outcome.
    DONE / FAIL / IDLE / WAIT are final on first occurrence — no retry.

    `provider_name` selects which `Provider` impl to instantiate via
    `provider.make_provider`. Required (no default) — every caller goes
    through the launcher, which resolves PHYSICLAW_PROVIDER.
    """
    for attempt in range(1, MAX_ATTEMPTS + 1):
        session = Session()
        await _run_session(triggers, provider_name=provider_name, session=session)
        if session.sentinel_status != STUCK:
            break
        if attempt < MAX_ATTEMPTS:
            log.warning(
                "session STUCK (attempt %d/%d): %r — retrying",
                attempt, MAX_ATTEMPTS, session.sentinel_recap,
            )


async def _run_session(
    triggers: list[Trigger],
    *,
    provider_name: str,
    session: Session,
) -> None:
    """One session attempt. Fresh sid / Trace / RawLog / MCP / Provider
    per call. Writes outcome to `session.sentinel_*`; never raises."""
    sid = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    provider: Provider | None = None
    tr: Trace | None = None
    rlog: RawLog | None = None
    try:
        # Open inside the try so the finally block's close() runs even
        # if construction fails midway (disk full, perms, etc.).
        tr = Trace(sid)
        rlog = RawLog(sid)
        tr.write({
            "event": "wake", "session": sid, "provider": provider_name,
            "triggers": [
                {"source": t.source, "description": t.description} for t in triggers
            ],
        })
        log.info(
            "wake session=%s provider=%s triggers=%s",
            sid, provider_name, [t.source or "?" for t in triggers],
        )
        mcp = await get_mcp()
        mcp_tools = await list_tools_cached()
        skill_registry = skill.discover()
        local_registry = builtin_tool.build_registry(skill_registry)
        local_schemas = builtin_tool.schemas(local_registry)
        # Full merged list goes to provider.chat(tools=) for invocation;
        # the inline `## Tooling` card pulls MCP names from AST so it
        # stays complete even offline. Each source has one consumer.
        tool_schemas = list(mcp_tools) + local_schemas
        schema_by_name = {s["name"]: s for s in tool_schemas}
        tr.write({
            "event": "tools_loaded",
            "mcp": [s["name"] for s in mcp_tools],
            "local": sorted(local_registry.keys()),
        })
        log.info(
            "tools loaded: %d MCP + %d local + %d skills",
            len(mcp_tools), len(local_registry), len(skill_registry),
        )

        system_prompt = prompt.render_system(
            local_tool_schemas=local_schemas,
            memory_ctx=memory.load_persistent(),
            cron_ctx=jobs.format_fired(triggers),
            skills_ctx=skill.render_section(skill_registry),
            provider_name=provider_name,
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": _format_triggers(triggers)},
        ]

        provider = make_provider(provider_name)
        prompt_hash = prompt.prefix_hash(messages)
        rlog.write_session_start(
            provider=provider_name,
            model=provider.model,
            prompt_hash=prompt_hash,
            tools=tool_schemas,
        )
        await _loop(
            mcp=mcp,
            provider=provider,
            messages=messages,
            tool_schemas=tool_schemas,
            schema_by_name=schema_by_name,
            local_registry=local_registry,
            session=session,
            prompt_hash=prompt_hash,
            tr=tr,
            rlog=rlog,
        )

        log.info(
            "session done: status=%s recap=%r",
            session.sentinel_status, session.sentinel_recap,
        )
        if session.sentinel_status == WAIT and not session.sentinel_turn_created_job:
            log.warning("WAIT with no create_job — auto-scheduling %d-min follow-up", WAIT_DEFAULT_MINUTES)
            _auto_schedule_wait_check(tr)

        tr.write({
            "event": "done",
            "sentinel": session.sentinel_status,
            "recap": session.sentinel_recap,
        })
    except asyncio.CancelledError:
        raise
    except Exception as e:
        # Crashes count as STUCK so the retry loop gives it another shot.
        log.exception("engine session crashed")
        if tr is not None:
            tr.write({"event": "crashed"})
        session.sentinel_status = STUCK
        session.sentinel_recap = f"session crashed: {e}"
    finally:
        if provider is not None:
            await provider.aclose()
        if tr is not None:
            tr.close()
        if rlog is not None:
            rlog.close()


# ---------- core loop ----------


async def _loop(
    *,
    mcp: McpClient,
    provider: Provider,
    messages: list[dict[str, Any]],
    tool_schemas: list[dict],
    schema_by_name: dict[str, dict],
    local_registry: dict[str, LocalTool],
    session: Session,
    prompt_hash: str,
    tr: Trace,
    rlog: RawLog,
) -> None:
    tr.write({"event": "prefix_pinned", "hash": prompt_hash})

    for turn in range(MAX_TURNS):
        # Plan tail is just-in-time: `messages[]` stays plan-free so the
        # prefix cache hits everything above the final user(<plan>) block.
        # Re-rendered every turn so update_progress tool calls take effect on
        # the NEXT request. request_messages is what the provider actually
        # sees; messages stays as canonical history.
        session.plan.tick_turn()
        request_messages = plan.inject_tail(messages, session.plan)
        tr.write({"event": "request", "turn": turn, "message_count": len(request_messages)})
        rlog.write_request(turn, request_messages)
        log.info("turn %d: %d messages → provider", turn + 1, len(request_messages))

        t0 = time.perf_counter()
        try:
            asst = await _chat_with_retry(provider, request_messages, tool_schemas)
        except Exception as e:
            log.exception("provider exhausted retries")
            tr.write({"event": "provider_failed", "turn": turn, "error": str(e)})
            session.sentinel_status = STUCK
            session.sentinel_recap = f"provider error: {e}"
            return

        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        rlog.write_response(turn, asst.raw, elapsed_ms=elapsed_ms)
        tr.write({
            "event": "response",
            "turn": turn,
            "finish_reason": asst.finish_reason,
            "content_len": len(asst.content or ""),
            "tool_calls": [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                for tc in asst.tool_calls
            ],
        })
        called = asst.tool_names()
        log.info(
            "turn %d: finish=%s calls=%s",
            turn + 1, asst.finish_reason, called or None,
        )

        # Principle 2: strip provider-specific fields before echoing back.
        messages.append(assistant_to_wire(asst))

        # Principle 3: route on finish_reason.
        if asst.finish_reason == FinishReason.CONTENT_FILTER:
            log.error("content_filter — stopping session")
            session.sentinel_status = FAIL
            session.sentinel_recap = "content filter blocked response"
            return

        # Shape checks may reject the turn. The rejected assistant message
        # must come back out of history: leaving orphan tool_calls behind
        # breaks providers' "tool_calls → matching tool messages" rule and
        # anchors the model on its own failure on retry.
        if not asst.tool_calls:
            messages.pop()
            messages.append({
                "role": "user",
                "content": (
                    "Your last turn had no tool_calls. Every turn must "
                    "either call tools or call end_session(status, recap) "
                    "to close. Reply again as a tool call — and include "
                    "note(summary=...) alongside."
                ),
            })
            continue

        # Turn shape: exactly [note, one-other]. `note` keeps a permanent
        # trace even after image tool_results are compacted away; the
        # one-other cap forces one action per turn.
        if len(called) != 2 or called.count("note") != 1:
            log.warning("turn %d: bad turn shape tool_calls=%s — injecting corrective", turn, called)
            tr.write({"event": "bad_turn_shape", "turn": turn, "tool_calls": called})
            messages.pop()
            messages.append({
                "role": "user",
                "content": (
                    f"Your last turn called {called!r}. Every turn must call "
                    "exactly two tools: `note` plus one other. Split any "
                    "extra work into separate turns. "
                    "Re-issue as [note, one-other]."
                ),
            })
            continue

        # Principle 6: each tool_call gets exactly one ToolResult, in order,
        # in the very next messages. Also mark truncation: if finish=length,
        # the final tool_call's arguments may be cut off — the validator
        # catches it and pairs an error result, same path as any bad args.
        if asst.finish_reason == FinishReason.LENGTH:
            log.warning("turn %d: finish=length; last tool_call args may be truncated", turn)
            tr.write({"event": "finish_length_warning", "turn": turn})

        for call in asst.tool_calls:
            result = await _dispatch(
                call=call,
                schema_by_name=schema_by_name,
                mcp=mcp,
                local_registry=local_registry,
                session=session,
                tr=tr,
                turn=turn,
            )
            messages.append(tool_result_to_wire(call, result))

        compact.drop_stale_screens(messages)

        if session.sentinel_status:
            return

    log.warning("engine hit max turns (%d)", MAX_TURNS)
    session.sentinel_status = STUCK
    session.sentinel_recap = f"max turns ({MAX_TURNS}) reached"


# ---------- dispatch ----------


async def _dispatch(
    *,
    call: ToolCall,
    schema_by_name: dict[str, dict],
    mcp: McpClient,
    local_registry: dict[str, LocalTool],
    session: Session,
    tr: Trace,
    turn: int,
) -> ToolResult:
    """Validate, then route to local handler or MCP. Always returns a
    ToolResult — never raises (principle 5 + principle 6 require that every
    ToolCall is paired with a ToolResult even on failure).
    """
    log.info("  → %s(%s)", call.name, brief_args(call.arguments))

    schema = schema_by_name.get(call.name)
    if schema is None:
        tr.write({"event": "tool_unknown", "turn": turn, "name": call.name, "id": call.id})
        log.warning("  ✗ %s: unknown tool", call.name)
        return ToolResult(
            tool_call_id=call.id,
            content=f"unknown tool: {call.name!r}",
            is_error=True,
        )

    # Principle 4: validate arguments before executing.
    try:
        validate_arguments(call.arguments, schema.get("input_schema") or {})
    except ValidationError as e:
        tr.write({
            "event": "tool_invalid_args", "turn": turn,
            "name": call.name, "id": call.id,
            "arguments": call.arguments, "error": str(e),
        })
        log.warning("  ✗ %s: invalid args — %s", call.name, e)
        return ToolResult(
            tool_call_id=call.id,
            content=f"invalid arguments for {call.name}: {e}",
            is_error=True,
        )

    local = local_registry.get(call.name)
    try:
        if local is not None:
            text = await local.handler(session, call.arguments)
            tr.write({
                "event": "tool_result", "turn": turn,
                "name": call.name, "id": call.id,
                "arguments": call.arguments, "text": text,
            })
            log.info("  ✓ %s → %s", call.name, brief(text, 80))
            return ToolResult(tool_call_id=call.id, content=text)

        blocks = await mcp.call_tool(call.name, call.arguments)
        content = blocks_to_tool_content(blocks)
        tr.write({
            "event": "tool_result", "turn": turn,
            "name": call.name, "id": call.id,
            "arguments": call.arguments, "blocks": blocks,
        })
        log.info("  ✓ %s → %s", call.name, brief_content(content))
        return ToolResult(tool_call_id=call.id, content=content)

    except Exception as e:
        log.error("  ✗ %s failed: %s", call.name, e)
        tr.write({"event": "tool_error", "turn": turn, "name": call.name, "error": str(e)})
        return ToolResult(
            tool_call_id=call.id,
            content=f"{call.name} failed: {e}",
            is_error=True,
        )


# ---------- helpers ----------


async def _chat_with_retry(
    provider: Provider, messages: list[dict], tools: list[dict],
) -> AssistantMessage:
    """Retry transient errors only (principle 3: permanent 4xx fails fast)."""
    last_err: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return await provider.chat(messages, tools)
        except ProviderTransientError as e:
            last_err = e
            if attempt < MAX_ATTEMPTS:
                log.warning("provider transient (attempt %d/%d): %s", attempt, MAX_ATTEMPTS, e)
                await asyncio.sleep(RETRY_BACKOFF)
    raise RuntimeError(f"provider failed after {MAX_ATTEMPTS} attempts: {last_err}")


def _format_triggers(triggers: list[Trigger]) -> str:
    # Leading `Now:` line is the absolute-date anchor — memory logs use
    # relative dates, and the model burns turns triangulating today without
    # it. Each trigger then uses a uniform `[stamp] source: body` envelope.
    now = dt.datetime.now()
    stamp = now.strftime("%Y-%m-%d %a %H:%M")
    lines = [
        f"Now: {stamp}",
        "",
        "[Current wake — act on this]",
    ]
    for t in triggers:
        source = t.source or "manual"
        lines.append(f"[{stamp}] {source}: {t.description}")
    return "\n".join(lines)


def _auto_schedule_wait_check(tr: Trace) -> None:
    """Schedule the singleton auto-WAIT-check job to fire in
    WAIT_DEFAULT_MINUTES. Reuses one canonical job id across sessions
    (see `jobs.upsert_auto_wait_check`) so jobs.md doesn't grow one
    entry per WAIT close.
    """
    at = dt.datetime.now() + dt.timedelta(minutes=WAIT_DEFAULT_MINUTES)
    try:
        jobs.upsert_auto_wait_check(at)
        tr.write({
            "event": "wait_auto_scheduled",
            "job_id": jobs.AUTO_WAIT_JOB_ID,
            "at": at.isoformat(timespec="minutes"),
        })
    except Exception as e:
        log.exception("failed to auto-schedule WAIT follow-up")
        tr.write({"event": "wait_auto_schedule_failed", "error": str(e)})
