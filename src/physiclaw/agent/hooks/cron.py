"""Cron hook + CLI — the "scheduler" half of the job system.

Each tick: load jobs.md → find jobs whose `Next fire time` has arrived →
fire one Trigger → update the status/timing fields in place. The agent
marks completion through the engine's `finish_job` tool; the CLI below
wraps the same function for manual maintenance. Terminal jobs
(cancel/done/fail) auto-purge after `[retention] trace_days`.

The durable data model (Job dataclass, parser, field updates) lives in
`physiclaw.agent.engine.job_store`. This file only owns:
  - the `@register`'d hook that does the ticking
  - the maintenance CLI (verify / jobs-to-do / purge / done / fail / cancel)
"""

import datetime as dt
import logging

from physiclaw.agent.engine import jobs as engine_jobs
from physiclaw.agent.engine.job_store import (
    JOBS_PATH,
    KIND_ONE_TIME,
    NEVER,
    STATUS_FIRED,
    Job,
    expire_stale_fired,
    find_due,
    format_minute,
    load_jobs,
    next_fire,
    purge_stale,
    update_fields,
)
from physiclaw.agent.runtime.hook import Trigger, register
from physiclaw.common.log_style import FailureState

log = logging.getLogger(__name__)
_load_failure = FailureState()
_purge_failure = FailureState()
_expiry_failure = FailureState()
_save_failure = FailureState()


# ---------- prompt building ----------


def _build_trigger_description(due: list[Job]) -> str:
    """Format due jobs into a Trigger description for the active engine."""
    blocks: list[str] = []
    for j in due:
        block = [f"job: {j.id}", f"description: {j.description}"]
        block.append(f"context: {j.context}")
        blocks.append("\n".join(block))

    body = "\n\n".join(blocks)
    ids = ", ".join(j.id for j in due)
    return (
        f"{body}\n\n"
        "When you finish each job, close it with the finish_job tool: mark done with a "
        "one-line result summary, or fail with what went wrong. Every "
        f"fired job needs an explicit close: {ids}"
    )


# ---------- hook ----------


@register
async def cron() -> Trigger | None:
    try:
        jobs = load_jobs()
    except Exception as error:
        log.log(
            _load_failure.level(error, logging.ERROR),
            "cron: failed to load %s",
            JOBS_PATH,
            exc_info=True,
            extra={
                "console_message": "Cannot read scheduled tasks — retrying automatically"
            },
        )
        return None
    if _load_failure.recovered():
        log.info("Scheduled tasks available again", extra={"console": True})
    if not jobs:
        return None

    now = dt.datetime.now()

    # Housekeeping: remove terminal jobs (cancel/done/fail) past the retention window.
    # Pass the already-loaded jobs so purge_stale doesn't re-parse jobs.md.
    try:
        purge_stale(now=now, jobs=jobs)
        if _purge_failure.recovered():
            log.info("Schedule cleanup restored", extra={"console": True})
    except Exception as error:
        log.log(
            _purge_failure.level(error, logging.ERROR),
            "cron: purge_stale failed",
            exc_info=True,
            extra={
                "console_message": "Old schedules could not be cleaned up — active tasks will still run"
            },
        )

    # Close firings left unfinished for 24h+: fail one-time jobs and re-arm
    # periodic jobs. Purge targets terminal statuses; expiry targets fired,
    # so both can share this snapshot. Re-armed jobs fire on the next tick,
    # when load_jobs sees their new pending status.
    try:
        expire_stale_fired(now=now, jobs=jobs)
        if _expiry_failure.recovered():
            log.info("Stale schedule recovery restored", extra={"console": True})
    except Exception as error:
        log.log(
            _expiry_failure.level(error, logging.ERROR),
            "cron: expire_stale_fired failed",
            exc_info=True,
            extra={
                "console_message": "Unfinished schedules could not be reset — retrying automatically"
            },
        )

    due = find_due(jobs, now)
    if not due:
        return None

    last_stamp = format_minute(now)
    updates: dict[str, dict[str, str]] = {}
    for j in due:
        fields: dict[str, str] = {
            "Last fire time": last_stamp,
            "Status": STATUS_FIRED,
        }
        if j.kind == KIND_ONE_TIME:
            fields["Next fire time"] = NEVER
        else:
            nxt = next_fire(j.schedule, now)
            if nxt is None:
                # Can't happen for the validated schedules jobs.md holds —
                # croniter always finds a next fire for a valid expression.
                raise RuntimeError(f"cron: no next fire for {j.id} ({j.schedule!r})")
            fields["Next fire time"] = format_minute(nxt)
        updates[j.id] = fields

    try:
        update_fields(JOBS_PATH, updates)
        if _save_failure.recovered():
            log.info("Schedule saving restored", extra={"console": True})
    except Exception as error:
        log.log(
            _save_failure.level(error, logging.ERROR),
            "cron: failed to write fire times to %s",
            JOBS_PATH,
            exc_info=True,
            extra={
                "console_message": "Cannot save schedule state — due tasks will run, but may run again"
            },
        )
        # Still fire — better to double-fire next tick than to miss entirely.

    description = _build_trigger_description(due)
    source = "cron:" + ",".join(j.id for j in due)
    return Trigger(
        description=description,
        source=source,
        label="; ".join(j.description for j in due),
    )


# ---------- CLI (used by the /cron skill and the agent) ----------


def _cli() -> int:
    import sys

    from physiclaw.common.log_style import configure_console, report_error

    configure_console("cli")
    args = sys.argv[1:]
    cmd = args[0] if args else "verify"

    if cmd == "verify":
        if not JOBS_PATH.exists():
            print(f"OK: {JOBS_PATH} does not exist yet (no jobs)")
            return 0
        try:
            jobs = load_jobs()
        except Exception as e:
            report_error(f"Cannot parse jobs: {e}")
            return 1
        print(f"OK: {len(jobs)} job(s) parsed from {JOBS_PATH}")
        for j in jobs:
            print(f"  [{j.kind:8s}] [{j.status:6s}] {j.id}")
            print(f"    {j.description}")
            print(f"    schedule: {j.schedule}")
            print(f"    next: {j.next_fire_time or NEVER}")
            print(f"    last: {j.last_fire_time or NEVER}")
            print(f"    exec: {j.execution_time or NEVER}")
            print(f"    result: {j.execution_result or NEVER}")
            print(f"    context: {j.context}")
            print()
        return 0

    if cmd == "jobs-to-do":
        try:
            jobs = load_jobs()
        except Exception as e:
            report_error(f"Cannot parse jobs: {e}")
            return 1
        fired = [j for j in jobs if j.status == STATUS_FIRED]
        if not fired:
            print("no jobs to do")
            return 0
        print(f"{len(fired)} job(s) fired, awaiting agent execution:")
        for j in fired:
            print(f"  [{j.kind}] {j.id}: {j.description}")
            print(f"    fired: {j.last_fire_time}")
        return 0

    if cmd in ("done", "fail", "cancel"):
        if len(args) < 2:
            print(
                f"usage: python -m physiclaw.agent.hooks.cron {cmd} <job-id> [result description]"
            )
            return 2
        job_id = args[1]
        try:
            jobs = load_jobs()
        except Exception as e:
            report_error(f"Cannot parse jobs: {e}")
            return 1
        job = next((j for j in jobs if j.id == job_id), None)
        if job is None:
            report_error(f"No job named {job_id!r} in {JOBS_PATH}")
            return 1

        # The status/reset/stamp policy is single-sourced in
        # engine.jobs.finish_job — this CLI only owns argv parsing and
        # its pinned printed surface.
        result_desc = " ".join(args[2:])
        try:
            engine_jobs.finish_job(id=job_id, status=cmd, recap=result_desc)
        except ValueError as e:
            report_error(str(e))
            return 1
        except Exception as e:
            report_error(f"Cannot save jobs: {e}")
            return 1
        print(f"OK: {cmd} {job_id}")
        if cmd in ("done", "fail") and job.kind != KIND_ONE_TIME:
            # Same warning the in-process finish_job tool gives: agents
            # read "done" as "over" while the periodic job re-arms and
            # fires again minutes later.
            nxt = job.next_fire_time or "its next scheduled minute"
            print(
                f"NOTE: {job_id} is PERIODIC — re-armed, fires again at "
                f"{nxt}. If its purpose is over, run: "
                f"python -m physiclaw.agent.hooks.cron cancel {job_id}"
            )
        return 0

    if cmd == "purge":
        try:
            jobs = load_jobs()
        except Exception as e:
            report_error(f"Cannot parse jobs: {e}")
            return 1
        try:
            # Background housekeeping tolerates unreadable job files. The
            # CLI must report them instead of claiming there is nothing to do.
            purged = purge_stale(jobs=jobs)
        except Exception as e:
            report_error(f"Cannot save jobs: {e}")
            return 1
        if not purged:
            print("nothing to purge")
        else:
            print(f"purged {len(purged)} stale job(s): {', '.join(purged)}")
        return 0

    print(
        "usage: python -m physiclaw.agent.hooks.cron [verify|jobs-to-do|purge|done|fail|cancel] [<id>]"
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
