# Jobs

Jobs use five-field cron schedules; their context is injected when they fire.

- Use **one-time** jobs for reminders, deferrals, and reply checks. If unresolved, schedule the next check then; do not create periodic reply watchers.
- Use **periodic** jobs only for recurring routines, at least 30 minutes apart. `finish_job(done|fail)` closes one occurrence; `cancel` stops recurrence.
- Explicitly `finish_job(id, status, recap)` for every fired job; closing a session does not finish them. Use `list_jobs("fired")` to recover unfinished work.
- IDs must be unique, using lowercase letters/digits/hyphens (e.g. `alice-groceries-2026-10-25`). Jobs are immutable: cancel the old job and create a new ID to reschedule.
