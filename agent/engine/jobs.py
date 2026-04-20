"""Engine-side job integration.

In-process replacement for the bash-CLI loopback `claude -p` used. This
module:
  - extracts cron-fired job ids from Triggers,
  - formats firings into the SYSTEM prompt,
  - persists outcomes to jobs.md when the sentinel fires,
  - creates new jobs when the model's response includes `create_cron`.

Data model + jobs.md I/O lives in `agent.jobs`. The hook that actually ticks
and fires is `agent.hooks.cron`.
"""
import datetime as dt
import logging

from agent.jobs import (
    ID_RE,
    JOBS_PATH,
    KIND_ONE_TIME,
    KIND_PERIODIC,
    STATUS_CANCEL,
    STATUS_DONE,
    STATUS_FAIL,
    STATUS_PEND,
    format_minute,
    load_jobs,
    next_fire,
    update_fields,
    validate_schedule,
)
from agent.runtime.hook import Trigger
from agent.runtime.sentinel import DONE, FAIL, STUCK

log = logging.getLogger(__name__)

_CRON_PREFIX = "cron:"
_FAIL_RECAP = {STUCK: "stuck", FAIL: "failed"}


def fired_job_ids(triggers: list[Trigger]) -> list[str]:
    """Extract cron job ids from Trigger.source values like 'cron:a,b'."""
    ids: list[str] = []
    for t in triggers:
        if not t.source or not t.source.startswith(_CRON_PREFIX):
            continue
        ids.extend(x for x in t.source[len(_CRON_PREFIX):].split(",") if x)
    return ids


def format_fired(triggers: list[Trigger]) -> str:
    """Build a SYSTEM-prompt section describing the cron jobs firing now."""
    ids = fired_job_ids(triggers)
    if not ids:
        return ""
    try:
        jobs = {j.id: j for j in load_jobs()}
    except Exception:
        log.exception("jobs: failed to load jobs.md")
        return ""

    blocks: list[str] = []
    for jid in ids:
        j = jobs.get(jid)
        if j is None:
            continue
        blocks.append(
            f"### {j.id}\n"
            f"{j.description}\n\n"
            f"Context: {j.context}"
        )
    if not blocks:
        return ""
    return "## Scheduled jobs firing now\n\n" + "\n\n".join(blocks)


def mark_outcome(
    triggers: list[Trigger], sentinel: str | None, recap: str
) -> None:
    """Persist the session outcome to jobs.md for each fired cron job.

    DONE / STUCK / FAIL all consume a one-time job (status → done or fail) and
    reset a periodic job to pend. WAIT leaves the job in fired status; the
    engine's auto-schedule covers the next check. IDLE is a no-op.
    """
    ids = fired_job_ids(triggers)
    if not ids or not sentinel:
        return
    try:
        jobs = {j.id: j for j in load_jobs()}
    except Exception:
        log.exception("jobs: failed to load jobs.md for outcome")
        return

    stamp = format_minute(dt.datetime.now())
    updates: dict[str, dict[str, str]] = {}

    for jid in ids:
        j = jobs.get(jid)
        if j is None:
            continue
        fields: dict[str, str] = {}
        if sentinel == DONE:
            fields["Execution time"] = stamp
            fields["Execution result"] = recap or "done"
            fields["Status"] = STATUS_DONE if j.kind == KIND_ONE_TIME else STATUS_PEND
        elif sentinel in (STUCK, FAIL):
            fields["Execution time"] = stamp
            fields["Execution result"] = recap or _FAIL_RECAP[sentinel]
            fields["Status"] = STATUS_FAIL if j.kind == KIND_ONE_TIME else STATUS_PEND
        else:
            # WAIT / IDLE — no jobs.md mutation here.
            continue
        updates[jid] = fields

    if updates:
        try:
            update_fields(JOBS_PATH, updates)
        except Exception:
            log.exception("jobs: failed to persist outcome to jobs.md")


def create_job(
    *,
    id: str,
    description: str,
    schedule: str,
    context: str,
    kind: str = KIND_ONE_TIME,
) -> None:
    """Append a new job section to jobs.md. Model-facing, called when the
    response carries a `create_cron` object.

    Raises ValueError on duplicate id, invalid kind, invalid schedule, or
    when required fields don't meet the parser's constraints.
    """
    if not ID_RE.match(id):
        raise ValueError(f"invalid job id {id!r} (lowercase + digits + hyphens)")
    if kind not in (KIND_ONE_TIME, KIND_PERIODIC):
        raise ValueError(f"kind must be one-time or periodic, got {kind!r}")
    validate_schedule(schedule)
    if len(context.strip()) < 10:
        raise ValueError("context must be at least 10 characters")
    description = description.strip()
    if not description:
        raise ValueError("description is required")

    if any(j.id == id for j in load_jobs()):
        raise ValueError(f"job id already exists: {id!r}")

    now = dt.datetime.now()
    nxt = next_fire(schedule, now)
    stamp_now = format_minute(now)
    stamp_next = format_minute(nxt) if nxt else "(never)"

    block = (
        f"\n## {id}\n"
        f"{description}\n\n"
        f"- Type: {kind}\n"
        f"- Status: {STATUS_PEND}\n"
        f"- Schedule: `{schedule}`\n"
        f"- Context: {context.strip()}\n"
        f"- Create time: {stamp_now}\n"
        f"- Next fire time: {stamp_next}\n"
        f"- Last fire time: (never)\n"
        f"- Execution time: (never)\n"
        f"- Execution result: (never)\n"
    )
    JOBS_PATH.parent.mkdir(parents=True, exist_ok=True)
    existed = JOBS_PATH.exists()
    with open(JOBS_PATH, "a") as f:
        if not existed:
            f.write("# Jobs\n")
        f.write(block)
    log.info("jobs: created %s (schedule=%r, next=%s)", id, schedule, stamp_next)


def cancel_job(id: str) -> None:
    """Set Status: cancel on an existing job. Raises ValueError on unknown id.
    No-op (returns silently) if the job is already cancelled.
    """
    existing = {j.id: j for j in load_jobs()}
    if id not in existing:
        raise ValueError(f"no job with id {id!r}")
    if existing[id].status == STATUS_CANCEL:
        return
    update_fields(JOBS_PATH, {id: {"Status": STATUS_CANCEL}})
    log.info("jobs: cancelled %s", id)
