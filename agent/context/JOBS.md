# Jobs

Scheduled work lives in `jobs/jobs.md`. Each job has an id, a 5-field
cron schedule, and a context blob the engine injects into the SYSTEM
prompt when the job fires.

## Lifecycle

The two `kind`s diverge at `finish_job`:

**One-time** — fires once, then terminates. Use for follow-ups,
reminders, deferred actions. Auto-purged from jobs.md 7 days after
termination.

```
create_job(kind="one-time")
    │
    ▼
[pend] ──(cron fires)──▶ [fired] ──(finish_job)──▶ [done|fail|cancel]
                                                         │
                                                         ▼
                                              auto-purged after 7 days
```

**Periodic** — fires every cycle until explicitly cancelled. Use for
recurring tasks, daily checks, monitoring.

```
create_job(kind="periodic")
    │
    ▼                ┌──────────────────────────────────────┐
[pend] ─(cron fires)─▶ [fired] ─(finish_job done/fail)─▶ [pend] ─▶ ...
                          │
                          └─(finish_job cancel)─▶ [cancel]  (permanent)
```

Key difference: `finish_job(id, "done", recap)` on a **one-time**
terminates the job; on a **periodic** it resets Status to `pend` so
the next scheduled cycle still fires (Next fire time was already
advanced when the cron hook fired). Only `finish_job(id, "cancel",
recap)` ends a periodic for good. Same for `"fail"` — it's "this
cycle failed, try again next cycle" for periodic, "this job failed,
done forever" for one-time.

**You own outcome marking.** The engine never auto-marks jobs at
session close. Every fired job in this wake needs an explicit
`finish_job(id, status, recap)` from you. A single wake can fire
multiple jobs and process them with different outcomes — explicit
per-job marking is the only way this works. Recap is one line, stored
as Execution result.

If you forget, the job sits in `fired` status indefinitely (it won't
be auto-cleaned — `purge_stale` only sweeps terminal jobs). On the
next wake, run `list_jobs("fired")` if you suspect orphaned jobs and
finish them then.

## Jobs are immutable — to change, finish + create

There is no `update_job`. Jobs are append-only: once created, the only
state change is the agent finishing them with `finish_job(id, status,
recap)`. To "edit" a job (reschedule, change context, revive after
cancel), the pattern is:

1. `finish_job(old_id, "cancel", "rescheduling — new id <new_id>")`
2. `create_job(new_id, description, new_schedule, new_context, kind?)`

Use a fresh id for the replacement (e.g. `remind-foo` →
`remind-foo-v2`); duplicate ids are rejected even when the prior entry
is terminal. Old terminal entries auto-purge from jobs.md after 7 days
of inactivity.

## When to use what

| Want to... | Use |
|---|---|
| Schedule a follow-up | `create_job(id, ...)` |
| "Edit" a job (any change) | `finish_job(old_id, "cancel", recap)` then `create_job(new_id, ...)` |
| Mark a fired job's outcome | `finish_job(id, status, recap)` |
| See full details of one job | `get_job(id)` |
| List jobs (one-liners) | `list_jobs(status?)` |

## Tools

- `create_job(id, description, schedule, context, kind?)` — append a
  new job. `kind` is `one-time` (default) or `periodic`. Use on WAIT
  to set the resume check (see CONVENTION.md), or when the owner asks
  for a recurring task. Raises on duplicate id (even if the existing
  entry is terminal — pick a fresh id).
- `get_job(id)` — return all fields of one job (description, type,
  status, schedule, context, fire times). Use when `list_jobs`'
  one-line summary isn't enough.
- `list_jobs(status?)` — inspect scheduled jobs as one-liners.
  Optional filter: one of `pend` / `fired` / `cancel` / `done` /
  `fail`, or `all` (default).
- `finish_job(id, status, recap)` — terminate a job. `status` is
  `done` (work complete), `fail` (blocked or impossible), or `cancel`
  (no longer needed; owner changed mind, the underlying task already
  happened, or you're rescheduling via cancel + new create_job).
  `recap` is one line. Raises on already-terminal jobs.
