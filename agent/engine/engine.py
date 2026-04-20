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
from typing import Any

from agent.engine import builtin_tool, compact, jobs, memory, prompt, skill
from agent.engine.builtin_tool import LocalTool, Session
from agent.engine.mcp_tool import McpClient
from agent.engine.dto import AssistantMessage, FinishReason, ToolCall, ToolResult
from agent.engine.provider import (
    Provider,
    ProviderTransientError,
    assistant_to_wire,
    blocks_to_tool_content,
    make_provider,
    tool_result_to_wire,
)
from agent.engine.trace import RawLog, Trace, brief, brief_args, brief_content
from agent.engine.validator import ValidationError, validate_arguments
from agent.runtime.hook import Trigger
from agent.runtime.sentinel import WAIT

log = logging.getLogger(__name__)

MAX_TURNS = 40
MAX_ATTEMPTS = 3
RETRY_BACKOFF = 5.0
WAIT_DEFAULT_MINUTES = 15


async def run(
    triggers: list[Trigger], *, provider_name: str
) -> None:
    """One engine session. Drives model ↔ tools loop until `end_session`
    closes it or a budget (MAX_TURNS / provider retries) is exhausted.

    `provider_name` selects which `Provider` impl to instantiate via
    `provider.make_provider`. Required (no default) — every caller goes
    through the launcher, which resolves PHYSICLAW_PROVIDER.
    """
    sid = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    provider: Provider | None = None
    session = Session()
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
        async with McpClient() as mcp:
            mcp_tools = await mcp.list_tools()
            skill_registry = skill.discover()
            local_registry = builtin_tool.build_registry(skill_registry)
            tool_schemas = _merge_schemas(mcp_tools, local_registry)
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
                memory_ctx=memory.load_context(),
                cron_ctx=jobs.format_fired(triggers),
                skills_ctx=skill.render_section(skill_registry),
            )
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": _format_triggers(triggers)},
            ]

            provider = make_provider(provider_name)
            await _loop(
                mcp=mcp,
                provider=provider,
                messages=messages,
                tool_schemas=tool_schemas,
                schema_by_name=schema_by_name,
                local_registry=local_registry,
                session=session,
                tr=tr,
                rlog=rlog,
            )

        log.info(
            "session done: status=%s recap=%r",
            session.sentinel_status, session.sentinel_recap,
        )
        jobs.mark_outcome(triggers, session.sentinel_status, session.sentinel_recap)
        if session.sentinel_status == WAIT and not session.sentinel_turn_created_cron:
            log.warning("WAIT with no create_cron — auto-scheduling %d-min follow-up", WAIT_DEFAULT_MINUTES)
            _auto_schedule_wait_check(sid, tr)

        tr.write({
            "event": "done",
            "sentinel": session.sentinel_status,
            "recap": session.sentinel_recap,
        })
    except Exception:
        log.exception("engine session crashed")
        if tr is not None:
            tr.write({"event": "crashed"})
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
    tr: Trace,
    rlog: RawLog,
) -> None:
    # SYSTEM is immutable after bootstrap; hash once at pin time and don't
    # recompute per turn. Any mutation would be a bug upstream (a caller
    # touching messages[0]), so one assertion at entry is enough.
    pinned = prompt.prefix_hash(messages)
    tr.write({"event": "prefix_pinned", "hash": pinned})

    for turn in range(MAX_TURNS):
        tr.write({"event": "request", "turn": turn, "message_count": len(messages)})
        rlog.write_request(turn, messages)
        log.info("turn %d: %d messages → provider", turn + 1, len(messages))

        try:
            asst = await _chat_with_retry(provider, messages, tool_schemas)
        except Exception as e:
            log.exception("provider exhausted retries")
            tr.write({"event": "provider_failed", "turn": turn, "error": str(e)})
            session.sentinel_status = "STUCK"
            session.sentinel_recap = f"provider error: {e}"
            return

        rlog.write_response(turn, asst.raw)
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
        log.info(
            "turn %d: finish=%s calls=%s",
            turn + 1, asst.finish_reason,
            [tc.name for tc in asst.tool_calls] or None,
        )

        # Principle 2: strip provider-specific fields before echoing back.
        messages.append(assistant_to_wire(asst))

        # Principle 3: route on finish_reason.
        if asst.finish_reason == FinishReason.CONTENT_FILTER:
            log.error("content_filter — stopping session")
            session.sentinel_status = "FAIL"
            session.sentinel_recap = "content filter blocked response"
            return

        if not asst.tool_calls:
            # Model ended the turn with only text. Every turn must act or
            # close. Inject corrective and re-request; the prefix stays
            # byte-identical, and the conversation tree stays replayable.
            messages.append({
                "role": "user",
                "content": (
                    "Your last turn had no tool_calls. Every turn must "
                    "either call tools or call end_session(status, recap) "
                    "to close. Reply again as a tool call."
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

        compact.prior_image(messages)

        if session.sentinel_status:
            return

    log.warning("engine hit max turns (%d)", MAX_TURNS)
    session.sentinel_status = "STUCK"
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


# ---------- adapters ----------


def _merge_schemas(
    mcp_tools: list[dict], local_registry: dict[str, LocalTool]
) -> list[dict]:
    out = list(mcp_tools)
    for lt in local_registry.values():
        out.append({
            "name": lt.name,
            "description": lt.description,
            "input_schema": lt.input_schema,
        })
    return out


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
    lines = ["Wake triggers:"]
    for t in triggers:
        tag = f"[{t.source}] " if t.source else ""
        lines.append(f"- {tag}{t.description}")
    return "\n".join(lines)


def _auto_schedule_wait_check(sid: str, tr: Trace) -> None:
    now = dt.datetime.now()
    at = now + dt.timedelta(minutes=WAIT_DEFAULT_MINUTES)
    schedule = f"{at.minute} {at.hour} {at.day} {at.month} *"
    job_id = f"wait-check-{sid.lower()}"
    try:
        jobs.create_job(
            id=job_id,
            description="Auto follow-up after WAIT with no explicit create_cron.",
            schedule=schedule,
            context="Previous session ended with WAIT. Re-check IM / state and continue the task.",
        )
        tr.write({
            "event": "wait_auto_scheduled",
            "job_id": job_id, "at": at.isoformat(timespec="minutes"),
        })
    except Exception as e:
        log.exception("failed to auto-schedule WAIT follow-up")
        tr.write({"event": "wait_auto_schedule_failed", "error": str(e)})
