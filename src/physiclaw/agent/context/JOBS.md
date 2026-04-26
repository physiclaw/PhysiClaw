# Jobs

Scheduled work lives in `jobs/jobs.md`. Each job has an id, a 5-field cron schedule, and a context blob the engine injects into your wake context when it fires.

## Lifecycle

```text
[pend] ──(cron fires)──▶ [fired] ──(finish_job)──▶ [done | fail | cancel]
```

Two kinds, diverging only at `finish_job`:

- **one-time** (default) — terminal. Auto-purged 7 days after termination. Use for follow-ups, reminders, deferred actions.
- **periodic** — `finish_job(id, "done"|"fail")` resets to `pend` for the next cycle (the next fire time was already advanced). Only `cancel` is permanent. Use for recurring tasks.

## Outcome marking

The engine never auto-marks. Every fired job in this wake needs an explicit `finish_job(id, status, recap)` from you. A wake may fire several jobs at once; mark each with its own outcome. Forgotten jobs sit in `fired` forever — `purge_stale` only sweeps terminal entries. On the next wake, `list_jobs("fired")` to find orphans.

## Id format

`<user>-<topic>-<YYYY-MM-DD>` — lowercase letters, digits, hyphens only. Example: `alice-water-plants-2026-05-01`. The date keeps recurring topics unique without `-v2` suffixes.

## Immutability

There is no `update_job`. To change a job (reschedule, edit context, revive after cancel):

1. `finish_job(old_id, "cancel", "rescheduling — new id <new_id>")`
2. `create_job(new_id, ...)` with a fresh id.

Duplicate ids are rejected even when the prior entry is terminal.

## Routing

| Want to...                  | Use                                            |
| --------------------------- | ---------------------------------------------- |
| Schedule a follow-up        | `create_job`                                   |
| Mark a fired job's outcome  | `finish_job`                                   |
| Edit / reschedule a job     | `finish_job(cancel)` + `create_job` (new id)   |
| See full details of one job | `get_job`                                      |
| List jobs (one-liners)      | `list_jobs(status?)`                           |
