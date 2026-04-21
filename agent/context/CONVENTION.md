# Convention

Use native tool_calls. `SCHEMA.md` is the fallback spec for runtimes
without native tool-calling; this file is what the engine uses today.

## Turn rules

- Every turn must either call tools or call `end_session` to close. A
  text-only response with no tool_calls stalls progress.
- After any view tool (`scan` / `peek` / `screenshot`), call
  `describe_view(description, curated_bbox)` before or alongside any
  other action. The raw image is dropped from history after the next
  turn — your description and curated_bbox become the only record of
  what you saw.

## Session close

Close with `end_session(status, recap)` where status is one of
DONE / STUCK / FAIL / IDLE / WAIT.

- On DONE / STUCK / FAIL, also call `append_log(entry)` with one line in
  the form `[HH:MM] app: page → page — what you did`.
- On WAIT, call `create_cron(...)` to schedule the resume check. If you
  don't, a 15-minute follow-up is scheduled automatically.

## Memory and jobs

- `save_memory(text)` — append a durable fact (when the owner says
  "remember this" or a lasting preference comes up).
- `update_memory(old, new)` — replace or remove a line in memory.
  `old` must match exactly one place; empty `new` deletes the line.
- `read_memory()` — re-read memory.md plus the last 3 daily logs.
- `list_jobs(status?)` / `cancel_cron(id)` — inspect or stop scheduled
  jobs.

## Skills

Invoke via the `Skill` tool: `Skill(name="wechat")`.
