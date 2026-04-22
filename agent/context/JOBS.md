# Jobs

Scheduled work lives in `jobs/jobs.md`. Each job has an id, a 5-field
cron schedule, and a context blob the engine injects into the SYSTEM
prompt when the job fires.

- `create_cron(id, description, schedule, context, kind?)` — schedule
  a follow-up. `kind` is `one-time` (default) or `periodic`. Use on
  WAIT to set the resume check (see CONVENTION.md), or when the owner
  asks for a recurring task.
- `list_jobs(status?)` — inspect scheduled jobs. Optional filter: one
  of `pend` / `fired` / `cancel` / `done` / `fail`, or `all`
  (default).
- `cancel_cron(id)` — stop a scheduled job (sets `Status: cancel`).
  Use when the owner says to stop an upcoming or recurring job.
