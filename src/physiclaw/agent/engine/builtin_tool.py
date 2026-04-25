"""Local (engine-side) tools, exposed via the same tools= API as MCP tools.

Each tool has:
  - name + description + JSONSchema (shared shape with MCP tools)
  - an async handler: (session, args) → ToolResult content

Tools defined here:
  - note — MUST ride alongside every turn's other tool calls.
  - update_progress — mutates `session.plan`, which is pinned at the tail of
    every provider request.
  - append_log / save_memory / read_memory / read_logs / update_memory
  - create_job / get_job / list_jobs / finish_job
  - wait — block the loop briefly (≤60s), for short in-session waits
    (the long-wait counterpart is `end_session(WAIT, …) + create_job`)
  - end_session — records sentinel status; engine exits after dispatch
  - Skill — fetch a SKILL.md body as text

`end_session` carries state back to the engine via the `Session` object;
`create_job` flags the session so the engine skips its auto-WAIT schedule.
Other handlers are stateless.
"""
import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from physiclaw.agent.engine import jobs, memory, skill
from physiclaw.agent.engine.plan import COMPLETED, IN_PROGRESS, PENDING, STATUS_ICON, Plan
from physiclaw.agent.engine.job_store import KIND_ONE_TIME, KIND_PERIODIC, NEVER, load_jobs
from physiclaw.agent.runtime.sentinel import STATUSES


@dataclass
class Session:
    """Ephemeral state the engine and local tools share for one session."""
    sentinel_status: str | None = None
    sentinel_recap: str = ""
    sentinel_turn_created_job: bool = False
    plan: Plan = field(default_factory=Plan)


Handler = Callable[[Session, dict], Awaitable[str]]


@dataclass(frozen=True)
class LocalTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Handler


# ---------- handlers ----------


async def _handle_note(_session: Session, args: dict) -> str:
    # The note's text lives in the assistant message's tool_calls arguments
    # (in history), not in session state. Handler only pairs the call with
    # a tool_result (principle 6) and surfaces a minimal ack.
    summary = (args.get("summary") or "").strip()
    return f"noted: {summary}"


async def _handle_update_progress(session: Session, args: dict) -> str:
    try:
        session.plan.update(**args)
    except ValueError as e:
        return f"update_progress rejected: {e}"
    return "progress updated"


async def _handle_append_log(_session: Session, args: dict) -> str:
    memory.append_log(args["entry"])
    return "log appended"


async def _handle_save_memory(_session: Session, args: dict) -> str:
    memory.save_fact(args["text"])
    return "fact saved to memory.md"


async def _handle_create_job(session: Session, args: dict) -> str:
    jobs.create_job(
        id=args["id"],
        description=args["description"],
        schedule=args["schedule"],
        context=args["context"],
        kind=args.get("kind", "one-time"),
    )
    session.sentinel_turn_created_job = True
    return f"scheduled job {args['id']!r}"


async def _handle_get_job(_session: Session, args: dict) -> str:
    job = jobs.get_job(args["id"])
    return (
        f"## {job.id}\n"
        f"{job.description}\n\n"
        f"- Type: {job.kind}\n"
        f"- Status: {job.status}\n"
        f"- Schedule: `{job.schedule}`\n"
        f"- Context: {job.context}\n"
        f"- Next fire time: {job.next_fire_time or NEVER}\n"
        f"- Last fire time: {job.last_fire_time or NEVER}\n"
        f"- Execution time: {job.execution_time or NEVER}\n"
        f"- Execution result: {job.execution_result or NEVER}\n"
    )


async def _handle_read_memory(_session: Session, _args: dict) -> str:
    out = memory.load_persistent()
    return out if out else "(memory.md is empty)"


async def _handle_read_logs(_session: Session, args: dict) -> str:
    n = args.get("entries", memory.DEFAULT_LOG_ENTRIES)
    out = memory.load_recent_entries(n)
    return out if out else "(no log entries found)"


async def _handle_update_memory(_session: Session, args: dict) -> str:
    memory.update_fact(args["old"], args["new"])
    return "memory.md updated"


async def _handle_list_jobs(_session: Session, args: dict) -> str:
    want = (args.get("status") or "all").lower()
    rows = load_jobs()
    if want != "all":
        rows = [j for j in rows if j.status == want]
    if not rows:
        suffix = f" with status={want!r}" if want != "all" else ""
        return f"no jobs{suffix}"
    lines = [f"{len(rows)} job(s):"]
    for j in rows:
        nxt = j.next_fire_time or NEVER
        lines.append(f"  [{j.kind}] [{j.status}] {j.id} — {j.description} (next: {nxt})")
    return "\n".join(lines)


async def _handle_finish_job(_session: Session, args: dict) -> str:
    jobs.finish_job(id=args["id"], status=args["status"], recap=args["recap"])
    return f"finished job {args['id']!r} as {args['status']}"


async def _handle_wait(_session: Session, args: dict) -> str:
    seconds = args["seconds"]
    await asyncio.sleep(seconds)
    return (
        f"waited {seconds}s — `peek` now to see what changed. "
        "Don't chain another `wait` without observing first; if still "
        "nothing, escalate with `end_session(WAIT, ...)` + `create_job`."
    )


async def _handle_end_session(session: Session, args: dict) -> str:
    status = args["status"]
    if status not in STATUSES:
        raise ValueError(
            f"status must be one of {sorted(STATUSES)}, got {status!r}"
        )
    session.sentinel_status = status
    session.sentinel_recap = args.get("recap", "").strip()
    return f"session closing: {status}"


def _handle_skill_factory(skill_registry: dict[str, skill.Skill]) -> Handler:
    async def _handle(_session: Session, args: dict) -> str:
        return skill.dispatch(skill_registry, args)
    return _handle


# ---------- tool definitions ----------


_NOTE = LocalTool(
    name="note",
    description=(
        "MUST be called on every turn, alongside whatever other tools you "
        "call. One line in `summary` saying what you're doing this turn "
        "and why. The summary survives compaction — it becomes the "
        "breadcrumb that labels any view image dropped from history, so "
        "write it to make sense of the surrounding tap / view in a "
        "transcript read cold."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "One line: what am I doing this turn and why.",
            },
        },
        "required": ["summary"],
    },
    handler=_handle_note,
)


_DONE = STATUS_ICON[COMPLETED]
_ACTIVE = STATUS_ICON[IN_PROGRESS]
_TODO = STATUS_ICON[PENDING]

_UPDATE_PROGRESS = LocalTool(
    name="update_progress",
    description=(
        "Mutate the working plan that's pinned at the tail of every "
        "request. Pass any subset of `owner_said` (the owner's "
        "verbatim ask), `understanding` (your one-line read of it), "
        "and `steps` (the full ordered step list — each step is "
        "`{content, status}`).\n"
        "\n"
        "Status is `pending` / `in_progress` / `completed`. **Exactly "
        "one step may be `in_progress` at a time** — the engine "
        "rejects plans that break this rule.\n"
        "\n"
        "Behavior rules — when to call (draft once → tick after each "
        "step → re-plan on shift), step granularity (one objective "
        "per step), the mandatory closing sequence (append_log → "
        "reply → go_back → home_screen → create_job → end_session, "
        "with append_log only on DONE/STUCK/FAIL and create_job only "
        "on WAIT), and the skip-if-≤2-steps rule — all live in "
        "CONVENTION § 'The plan'. Read it.\n"
        "\n"
        "Worked example — owner asks 'buy me some snacks on JD'. "
        "After opening JD and entering 7Fresh, the plan reads:\n"
        f"  {_DONE}Open JD via Spotlight\n"
        f"  {_DONE}Tap the 7Fresh grocery entry point\n"
        f"  {_ACTIVE}Search 'chips', tap first matching item, Add to cart\n"
        f"  {_TODO}Search 'cola', tap first matching item, Add to cart\n"
        f"  {_TODO}Open cart, Checkout, confirm shipping address\n"
        f"  {_TODO}Send order summary to owner in IM for confirm\n"
        f"  {_TODO}go_back to exit the chat thread\n"
        f"  {_TODO}home_screen\n"
        f"  {_TODO}create_job to resume after owner OK\n"
        f"  {_TODO}end_session WAIT\n"
        f"  {_TODO}[on resume] tap Pay, complete checkout\n"
        f"  {_TODO}append_log one line describing the outcome\n"
        f"  {_TODO}Reply to owner in IM: 'Order placed ✅'\n"
        f"  {_TODO}go_back to exit the chat thread\n"
        f"  {_TODO}home_screen\n"
        f"  {_TODO}end_session DONE\n"
        f"(`{_DONE.strip()}`=completed, `{_ACTIVE.strip()}`=in_progress, "
        f"`{_TODO.strip()}`=pending; you set the status values, the "
        "renderer picks the icons.)\n"
        "\n"
        "Pass only changed fields. First call: usually owner_said + "
        "understanding + steps. Subsequent calls: usually just steps."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "owner_said": {
                "type": "string",
                "minLength": 1,
                "maxLength": 1000,
                "description": "What the owner literally said — quote the IM message.",
            },
            "understanding": {
                "type": "string",
                "minLength": 5,
                "maxLength": 1000,
                "description": "One sentence: what you think they want and why.",
            },
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 1000,
                            "description": "One concrete imperative action.",
                        },
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed"],
                            "description": (
                                "pending = not started; in_progress = "
                                "currently doing (max ONE at a time); "
                                "completed = finished."
                            ),
                        },
                    },
                    "required": ["content", "status"],
                },
                "description": (
                    "Full ordered step list. Exactly one step may be "
                    "in_progress at a time; the engine rejects plans that "
                    "violate this."
                ),
            },
        },
        "required": [],
    },
    handler=_handle_update_progress,
)


_APPEND_LOG = LocalTool(
    name="append_log",
    description=(
        "Append one line after every major step (purchase placed, message "
        "sent, item added) and at session close on DONE/STUCK/FAIL — "
        "writes to today's memory/YYYY-MM-DD.md daily log. Per-step "
        "breadcrumbs survive session end, so a later wake can recover "
        "what's already done. See PERSISTENCE.md for the rationale."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "entry": {
                "type": "string",
                "description": "One line, format '[HH:MM] app: page -> page - what you did'.",
            },
        },
        "required": ["entry"],
    },
    handler=_handle_append_log,
)


_SAVE_MEMORY = LocalTool(
    name="save_memory",
    description=(
        "Append a durable fact or preference to memory/memory.md. Only when "
        "the owner says 'remember this' or you learned a lasting preference."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "text": {"type": "string"},
        },
        "required": ["text"],
    },
    handler=_handle_save_memory,
)


_CREATE_JOB = LocalTool(
    name="create_job",
    description=(
        "Schedule a follow-up job in jobs/jobs.md. Use for WAIT (to auto-resume), "
        "or when the owner asks for a recurring task."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "lowercase / digits / hyphens"},
            "description": {"type": "string"},
            "schedule": {"type": "string", "description": "5-field cron (min hour dom mon dow)"},
            "context": {"type": "string", "description": "at least 10 chars"},
            "kind": {"type": "string", "enum": [KIND_ONE_TIME, KIND_PERIODIC]},
        },
        "required": ["id", "description", "schedule", "context"],
    },
    handler=_handle_create_job,
)


_GET_JOB = LocalTool(
    name="get_job",
    description=(
        "Return all fields of a single job (description, type, status, "
        "schedule, context, fire times). Use when `list_jobs`' one-line "
        "summary isn't enough — e.g. owner asks what a queued reminder "
        "is about. Raises if the id does not exist."
    ),
    input_schema={
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
    },
    handler=_handle_get_job,
)


_READ_MEMORY = LocalTool(
    name="read_memory",
    description=(
        "Re-read `memory/memory.md` from disk. SYSTEM already shows it "
        "under `## memory.md` at session start; call this only after a "
        "`save_memory`/`update_memory` mid-session, before another "
        "`update_memory` whose `old` must match byte-exactly."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
    handler=_handle_read_memory,
)


_READ_LOGS = LocalTool(
    name="read_logs",
    description=(
        "Fetch the last N log entries across `memory/YYYY-MM-DD.md` "
        "files, most recent first. If today's file has fewer than N, "
        "walks back through prior days until N are collected. Each "
        "`[HH:MM]` is rewritten to `[YYYY-MM-DD HH:MM]` so cross-day "
        "order is unambiguous in the merged view. Daily logs are NOT "
        "auto-injected at wake — call this whenever you need recent "
        "activity context (yesterday's purchases, prior IM exchanges, "
        "open follow-ups)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "entries": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
                "description": (
                    f"How many recent entries to return (default "
                    f"{memory.DEFAULT_LOG_ENTRIES})."
                ),
            },
        },
        "required": [],
    },
    handler=_handle_read_logs,
)


_UPDATE_MEMORY = LocalTool(
    name="update_memory",
    description=(
        "Replace the single occurrence of `old` with `new` in memory/memory.md. "
        "If `new` is empty, the line containing `old` is removed. Errors if "
        "`old` is not found or matches more than one place — in that case, "
        "narrow `old` with enough surrounding text to pin exactly one match. "
        "SYSTEM's `## memory.md` block shows current contents byte-exact "
        "as of session start. After any prior save_memory / update_memory "
        "this session, that snapshot is stale — call `read_memory` first."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "old": {"type": "string", "description": "exact substring to replace"},
            "new": {"type": "string", "description": "replacement; empty string deletes the line"},
        },
        "required": ["old", "new"],
    },
    handler=_handle_update_memory,
)


_LIST_JOBS = LocalTool(
    name="list_jobs",
    description=(
        "List jobs from jobs/jobs.md. Optional `status` filter: all (default) "
        "/ pend / fired / cancel / done / fail. Returns a one-line summary "
        "per job."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["all", "pend", "fired", "cancel", "done", "fail"],
            },
        },
        "required": [],
    },
    handler=_handle_list_jobs,
)


_FINISH_JOB = LocalTool(
    name="finish_job",
    description=(
        "Mark a job as terminated. `status` is `done` (the work the job "
        "was meant to trigger is complete), `fail` (couldn't be done — "
        "blocked or impossible), or `cancel` (no longer needed; e.g. "
        "owner changed their mind, or the underlying task already "
        "happened). Call once per fired job during the session — there "
        "is no engine fallback; forgotten jobs sit in `fired` "
        "indefinitely. Periodic jobs reset to `pend` on done/fail "
        "(still fire next cycle); `cancel` is permanent. Raises on "
        "already-terminal jobs."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "status": {"type": "string", "enum": ["done", "fail", "cancel"]},
            "recap": {"type": "string", "description": "one-line outcome summary"},
        },
        "required": ["id", "status", "recap"],
    },
    handler=_handle_finish_job,
)


_WAIT = LocalTool(
    name="wait",
    description=(
        "Block briefly (1-60s), then return so your next turn can "
        "re-observe. For short in-session waits when the owner is "
        "actively engaged (e.g. you sent a message and they're typing). "
        "**After `wait`, your very next call must be `peek`** — chaining "
        "`wait → wait` without observing is a bug; if the first peek "
        "shows no change, escalate with `end_session(WAIT, ...)` + "
        "`create_job` rather than waiting again."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "seconds": {"type": "integer", "minimum": 1, "maximum": 60},
        },
        "required": ["seconds"],
    },
    handler=_handle_wait,
)


_END_SESSION = LocalTool(
    name="end_session",
    description=(
        "Close the session. `status` is DONE (complete, result verified), "
        "STUCK (blocker outside your control), FAIL (task cannot succeed), "
        "IDLE (wake triggered but no work needed), or WAIT (paused for "
        "owner reply — pair with create_job to auto-resume)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": sorted(STATUSES)},
            "recap": {"type": "string", "description": "one-line summary"},
        },
        "required": ["status", "recap"],
    },
    handler=_handle_end_session,
)


_SKILL_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "Skill name, e.g. 'wechat'.",
        },
        "reference": {
            "type": "string",
            "description": "Optional. Path under the skill's references/ "
                           "directory. Loads that file's text instead of "
                           "SKILL.md — use for details / edge cases the "
                           "SKILL.md body explicitly points at.",
        },
    },
    "required": ["name"],
}


def schemas(registry: dict[str, LocalTool]) -> list[dict]:
    """Flatten a local-tool registry into wire-shape dicts (the same shape
    as MCP `list_tools()` entries)."""
    return [
        {
            "name": lt.name,
            "description": lt.description,
            "input_schema": lt.input_schema,
        }
        for lt in registry.values()
    ]


def build_registry(
    skill_registry: dict[str, skill.Skill],
) -> dict[str, LocalTool]:
    """All local tools keyed by name. Skill is included iff skills were
    discovered (keeps the tool surface minimal when no skills exist).

    **Insertion order matters.** `schemas()` iterates `.values()`, and the
    engine concatenates MCP tools + these to build `tool_schemas`, so the
    order here directly determines the order of `tools[]` in the provider
    request. LLMs show mild position bias — the turn-shape primitives
    (`note`, `update_progress`) are listed first so they sit near the top
    of the array. Don't reshuffle, and don't refactor this dict into a
    set or comprehension.
    """
    tools: dict[str, LocalTool] = {
        _NOTE.name: _NOTE,
        _UPDATE_PROGRESS.name: _UPDATE_PROGRESS,
        _APPEND_LOG.name: _APPEND_LOG,
        _SAVE_MEMORY.name: _SAVE_MEMORY,
        _READ_MEMORY.name: _READ_MEMORY,
        _READ_LOGS.name: _READ_LOGS,
        _UPDATE_MEMORY.name: _UPDATE_MEMORY,
        _CREATE_JOB.name: _CREATE_JOB,
        _GET_JOB.name: _GET_JOB,
        _LIST_JOBS.name: _LIST_JOBS,
        _FINISH_JOB.name: _FINISH_JOB,
        _WAIT.name: _WAIT,
        _END_SESSION.name: _END_SESSION,
    }
    if skill_registry:
        tools["Skill"] = LocalTool(
            name="Skill",
            description=(
                f"Load a skill's workflow ({'/'.join(sorted(skill_registry))}) "
                "before acting in that app — don't tap blind. Default "
                "returns the skill's SKILL.md body for you to follow. "
                "Pass `reference=<path>` to load an on-demand details file "
                "from the skill's references/ directory — use it when the "
                "body explicitly points at a reference."
            ),
            input_schema=_SKILL_SCHEMA,
            handler=_handle_skill_factory(skill_registry),
        )
    return tools


