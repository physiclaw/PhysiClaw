"""Local (engine-side) tools, exposed via the same tools= API as MCP tools.

Each tool has:
  - name + description + JSONSchema (shared shape with MCP tools)
  - an async handler: (session, args) → ToolResult content

Tools defined here:
  - describe_view / append_log / save_memory / read_memory / update_memory
  - create_cron / list_jobs / cancel_cron
  - end_session — records sentinel status; engine exits after dispatch
  - Skill — fetch a SKILL.md body as text

`end_session` carries state back to the engine via the `Session` object;
`create_cron` flags the session so the engine skips its auto-WAIT schedule.
Other handlers are stateless.
"""
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from agent.engine import jobs, memory, skill
from agent.jobs import KIND_ONE_TIME, KIND_PERIODIC, load_jobs
from agent.runtime.sentinel import STATUSES


@dataclass
class Session:
    """Ephemeral state the engine and local tools share for one session."""
    sentinel_status: str | None = None
    sentinel_recap: str = ""
    sentinel_turn_created_cron: bool = False


Handler = Callable[[Session, dict], Awaitable[str]]


@dataclass(frozen=True)
class LocalTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Handler


# ---------- handlers ----------


async def _handle_describe_view(_session: Session, args: dict) -> str:
    # The description + curated_bbox live in the assistant message the
    # model just emitted (history), not in session state. This handler's
    # only job is to acknowledge the recording so the engine pairs the
    # call with a tool_result (principle 6).
    desc = args["description"].strip()
    return f"recorded description ({len(desc)} chars, " \
           f"{len(args['curated_bbox'])} curated ids)"


async def _handle_append_log(_session: Session, args: dict) -> str:
    memory.append_log(args["entry"])
    return "log appended"


async def _handle_save_memory(_session: Session, args: dict) -> str:
    memory.save_fact(args["text"])
    return "fact saved to memory.md"


async def _handle_create_cron(session: Session, args: dict) -> str:
    jobs.create_job(
        id=args["id"],
        description=args["description"],
        schedule=args["schedule"],
        context=args["context"],
        kind=args.get("kind", "one-time"),
    )
    session.sentinel_turn_created_cron = True
    return f"scheduled job {args['id']!r}"


async def _handle_read_memory(_session: Session, _args: dict) -> str:
    ctx = memory.load_context()
    return ctx if ctx else "(memory is empty)"


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
        nxt = j.next_fire_time or "(never)"
        lines.append(f"  [{j.kind}] [{j.status}] {j.id} — {j.description} (next: {nxt})")
    return "\n".join(lines)


async def _handle_cancel_cron(_session: Session, args: dict) -> str:
    jobs.cancel_job(args["id"])
    return f"cancelled {args['id']}"


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


_DESCRIBE_VIEW = LocalTool(
    name="describe_view",
    description=(
        "MUST be called after any view tool (scan / peek / screenshot) "
        "returns an image. Records what you saw so the engine can compact "
        "the image out of history. Other tools may be called in the same "
        "turn after this one."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "2-4 sentences describing the current screen.",
            },
            "curated_bbox": {
                "type": "array",
                "description": "Subset of input bboxes worth keeping for "
                               "later turns. Each item references an id "
                               "from the input list.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "role": {"type": "string"},
                        "label": {"type": "string"},
                        "bbox": {
                            "type": "array",
                            "items": {"type": "number"},
                            "minItems": 4,
                        },
                    },
                    "required": ["id", "bbox"],
                },
                "minItems": 1,
            },
        },
        "required": ["description", "curated_bbox"],
    },
    handler=_handle_describe_view,
)


_APPEND_LOG = LocalTool(
    name="append_log",
    description=(
        "Append one line to today's memory/YYYY-MM-DD.md daily log. Use on "
        "DONE/STUCK/FAIL to record what you did."
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


_CREATE_CRON = LocalTool(
    name="create_cron",
    description=(
        "Schedule a follow-up job in jobs/jobs.md. Use for WAIT (to auto-resume), "
        "or when the user asks for a recurring task."
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
    handler=_handle_create_cron,
)


_READ_MEMORY = LocalTool(
    name="read_memory",
    description=(
        "Fetch memory/memory.md plus the last 3 daily logs. Daily logs are "
        "NOT auto-injected at wake — call this whenever you need recent "
        "activity context (yesterday's purchases, prior IM exchanges, "
        "open follow-ups). Persistent memory.md is already in your "
        "context but re-reading it here is cheap."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
    handler=_handle_read_memory,
)


_UPDATE_MEMORY = LocalTool(
    name="update_memory",
    description=(
        "Replace the single occurrence of `old` with `new` in memory/memory.md. "
        "If `new` is empty, the line containing `old` is removed. Errors if "
        "`old` is not found or matches more than one place — in that case, "
        "narrow `old` with enough surrounding text to pin exactly one match. "
        "Call `read_memory` first to see the current contents byte-exactly."
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


_CANCEL_CRON = LocalTool(
    name="cancel_cron",
    description=(
        "Cancel a scheduled job (sets Status: cancel in jobs.md). Use when "
        "the owner says to stop an upcoming or recurring job. Raises if the "
        "id does not exist."
    ),
    input_schema={
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
    },
    handler=_handle_cancel_cron,
)


_END_SESSION = LocalTool(
    name="end_session",
    description=(
        "Close the session. `status` is DONE (complete, result verified), "
        "STUCK (blocker outside your control), FAIL (task cannot succeed), "
        "IDLE (wake triggered but no work needed), or WAIT (paused for "
        "owner reply — pair with create_cron to auto-resume)."
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
    """
    tools: dict[str, LocalTool] = {
        _DESCRIBE_VIEW.name: _DESCRIBE_VIEW,
        _APPEND_LOG.name: _APPEND_LOG,
        _SAVE_MEMORY.name: _SAVE_MEMORY,
        _READ_MEMORY.name: _READ_MEMORY,
        _UPDATE_MEMORY.name: _UPDATE_MEMORY,
        _CREATE_CRON.name: _CREATE_CRON,
        _LIST_JOBS.name: _LIST_JOBS,
        _CANCEL_CRON.name: _CANCEL_CRON,
        _END_SESSION.name: _END_SESSION,
    }
    if skill_registry:
        tools["Skill"] = LocalTool(
            name="Skill",
            description=(
                "Progressive loader for skill workflows. Default returns "
                "the skill's SKILL.md body for you to follow. Pass "
                "`reference=<path>` to load an on-demand details file "
                "from the skill's references/ directory — use it when the "
                "body explicitly points at a reference."
            ),
            input_schema=_SKILL_SCHEMA,
            handler=_handle_skill_factory(skill_registry),
        )
    return tools


VIEW_TOOL_NAMES = frozenset({"scan", "peek", "screenshot"})
