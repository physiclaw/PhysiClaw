"""Local (engine-side) tools, exposed via the same tools= API as MCP tools.

Each tool has:
  - name + description + JSONSchema (shared shape with MCP tools)
  - an async handler: (session, args) → ToolResult content

Tools defined here:
  - note — MUST ride alongside every turn's other tool calls.
  - update_plan — mutates `session.plan`, which is pinned at the tail of
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

from agent.engine import jobs, memory, skill
from agent.engine.plan import Plan
from agent.engine.job_store import KIND_ONE_TIME, KIND_PERIODIC, NEVER, load_jobs
from agent.runtime.sentinel import STATUSES
from physiclaw.vision.util import validate_bbox


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
    screen = (args.get("screen") or "").strip()
    pinned = args.get("key_ui_elements") or {}
    # JSONSchema can't express left<right / top<bottom. Reject here so bad
    # pins fail loudly now, not at tap time many turns later.
    for semantic, spec in pinned.items():
        validate_bbox(spec["bbox"])
    parts = [f"noted: {summary}"]
    if screen:
        parts.append(f"screen: {len(screen)} chars")
    if pinned:
        parts.append(f"pinned {len(pinned)}: {', '.join(pinned)}")
    return " | ".join(parts)


async def _handle_update_plan(session: Session, args: dict) -> str:
    session.plan.update(**args)
    return "plan updated"


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
    days = args.get("days", memory.DAILY_LOOKBACK)
    out = memory.load_recent_activity(days)
    return out if out else f"(no daily logs in last {days} days)"


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
    return f"waited {seconds}s"


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
        "and why. Fill `screen` whenever a view tool just ran or you're "
        "about to take a physical action — that text becomes the permanent "
        "record after the raw image is dropped from history. Use "
        "`key_ui_elements` to pin bboxes that must survive the latest-screen-"
        "wins compaction (e.g. bboxes captured from a `screenshot` listing "
        "that a subsequent `peek` will bump out of history)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "One line: what am I doing this turn and why.",
            },
            "screen": {
                "type": "string",
                "description": (
                    "What I see on screen right now (app, page, relevant "
                    "elements). Fill on observation turns and physical-action "
                    "turns; omit for pure admin turns (read_logs, "
                    "update_plan, end_session)."
                ),
            },
            "key_ui_elements": {
                "type": "object",
                "description": (
                    "Working-set cache of actionable bboxes on the "
                    "current screen type — CTAs and nav anchors you'll "
                    "tap across multiple future turns. Typical pins for "
                    "a list page: search box, cart icon, footer tabs, "
                    "back arrow, + add-to-cart buttons, product rows "
                    "you plan to open. For a detail page: primary CTA "
                    "(add to cart / buy now), back, spec selector. "
                    "Pins survive compaction via note args, so you "
                    "don't re-ground every turn. DO NOT pin (a) the "
                    "bbox you're tapping THIS turn (it's already in the "
                    "tap args), (b) decoration you won't tap (prices, "
                    "timestamps, descriptive text next to a button), "
                    "(c) elements on a screen type you're leaving and "
                    "won't return to. Re-pin when the screen type "
                    "changes. Keys are slug-style handles you'll "
                    "reference on later turns (e.g. 'add_to_cart', "
                    "'search_box', 'row_3_plus'). Values carry the "
                    "matching listing row's kind + label + bbox. `bbox` "
                    "MUST be transcribed character-for-character from "
                    "the listing row — same digits, same order; `0.520` "
                    "stays `0.520`, don't shorten to `0.52` or drift to "
                    "`0.518`. Do not retype from memory, do not re-read "
                    "off the image. `label` is YOUR reading of what "
                    "that element is: fill for every entry (no "
                    "empties), correct garbled OCR if needed, and for "
                    "icons describe what it looks like. Cap: 8 pins "
                    "per turn."
                ),
                "maxProperties": 8,
                "propertyNames": {"pattern": "^[a-z][a-z0-9_]*$"},
                "additionalProperties": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": ["icon", "text"]},
                        "label": {"type": "string", "minLength": 1},
                        "bbox": {
                            "type": "array",
                            "items": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                            "minItems": 4,
                            "maxItems": 4,
                        },
                    },
                    "required": ["kind", "label", "bbox"],
                },
            },
        },
        "required": ["summary"],
    },
    handler=_handle_note,
)


_UPDATE_PLAN = LocalTool(
    name="update_plan",
    description=(
        "Update the session's working plan. The plan is pinned at the tail "
        "of every request, so you never lose track of the goal. Call as "
        "soon as you read what the owner wants, and again whenever the "
        "plan shifts (unexpected screen, owner adjusts, partial failure). "
        "Pass only the fields you want to change."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "owner_said": {
                "type": "string",
                "description": "What the owner literally said — quote the IM message.",
            },
            "understanding": {
                "type": "string",
                "description": "One sentence: what you think they want and why.",
            },
            "steps": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Ordered imperative steps to reach the goal.",
            },
        },
        "required": [],
    },
    handler=_handle_update_plan,
)


_APPEND_LOG = LocalTool(
    name="append_log",
    description=(
        "Append one line to today's memory/YYYY-MM-DD.md daily log. Call "
        "after every major step (purchase, message sent, item added) AND "
        "once at session close on DONE/STUCK/FAIL. See PERSISTENCE.md "
        "for the rationale."
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
        "Fetch the last N daily logs (`memory/YYYY-MM-DD.md`). Daily logs "
        "are NOT auto-injected at wake — call this whenever you need "
        "recent activity context (yesterday's purchases, prior IM "
        "exchanges, open follow-ups)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "days": {
                "type": "integer",
                "minimum": 1,
                "maximum": 30,
                "description": (
                    f"Lookback window in days (default "
                    f"{memory.DAILY_LOOKBACK})."
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
        "For longer waits, close with `end_session(WAIT, ...)` + "
        "`create_job` instead."
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
    """
    tools: dict[str, LocalTool] = {
        _NOTE.name: _NOTE,
        _UPDATE_PLAN.name: _UPDATE_PLAN,
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


