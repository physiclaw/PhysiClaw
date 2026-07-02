# Jobs

Scheduled work lives in `jobs/jobs.md`: id + 5-field cron + a context blob injected at wake under `## Scheduled jobs firing now`.

## Lifecycle

```text
[pend] ──(cron fires)──▶ [fired] ──(finish_job)──▶ [done | fail | cancel]
```

- **one-time** (default) — terminal; auto-purged 7 days after termination. For follow-ups, reminders, deferred actions.
- **periodic** — `finish_job(id, "done"|"fail")` resets to `pend` (next fire time already advanced); only `cancel` is permanent.

## Outcome marking

The engine never auto-marks. Every fired job needs its own `finish_job(id, status, recap)` this wake — a wake may fire several. Unmarked jobs sit in `fired` forever (`purge_stale` sweeps only terminal). Next wake: `list_jobs("fired")` to find orphans.

## Ids & immutability

Id: `<user>-<topic>-<YYYY-MM-DD>`, lowercase letters/digits/hyphens (e.g. `alice-water-plants-2026-05-01`) — the date keeps recurring topics unique. There is no `update_job`, and duplicate ids are rejected even against terminal entries. To reschedule / edit / revive:

1. `finish_job(old_id, "cancel", "rescheduling — new id <new_id>")`
2. `create_job(new_id, ...)`
