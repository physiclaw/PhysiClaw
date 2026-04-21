# Convention

Use native tool_calls. `SCHEMA.md` is the fallback spec for runtimes
without native tool-calling; this file is what the engine uses today.

## Turn rules

- **Every turn is exactly two tool calls: `note` plus one other.** No
  more, no less. `note.summary` is one line saying what you're doing this
  turn and why. Fill `note.screen` whenever a view tool just ran or
  you're about to take a physical action — that text becomes the
  permanent record after the raw image is dropped from history.
- Split admin across separate turns: `append_log` → next turn
  `end_session`. `save_memory` → next turn `append_log` → next turn
  `end_session`. Each close-out step is its own `[note, one-other]`
  turn.
- A turn with zero or text-only tool_calls stalls the loop — always
  emit `[note, one-other]` or `[note, end_session]` to close.

## The plan

The engine keeps a working plan on the session and pins it at the tail
of every request — you will see a `<plan>...</plan>` block as the last
message on every turn.

- On wake the plan says "IM hasn't been checked yet — open IM first."
- Once you read the owner's message, call `update_plan(owner_said,
  understanding, steps)` to replace the seed with the real task.
- Whenever the plan shifts (unexpected screen, owner adjusts, partial
  failure), call `update_plan` again. Only pass fields you want to
  change.

## Compaction: latest screen wins

Only the most recent `scan` / `peek` / `screenshot` result survives in
history. Earlier observations are deleted — the engine drops the whole
assistant + tool_result pair. Your `note.screen` from subsequent
physical-action turns preserves what each earlier image showed as text.

Consequence: don't rely on a `peek` from three turns ago to plan the
current tap. If you need to re-check, re-observe — it's cheap.

## Bboxes come from the listing, never from eyeballing

Every physical-action bbox must be copied verbatim from a bbox in the
most recent `scan` / `peek` / `screenshot` listing. Never guess, never
round, never average two listing rows, never "eyeball" coordinates from
an image. If the element you want isn't in the current listing, re-scan
— don't fabricate coords.

This is what makes `sequence` safe: each step's bbox is grounded in the
listing that was live when you planned the chain. A made-up bbox turns
`sequence` into blind tapping and compounds errors step by step.

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
