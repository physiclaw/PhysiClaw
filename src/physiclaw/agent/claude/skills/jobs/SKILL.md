---
name: jobs
description: Use when the task involves scheduling future work — any "remind me at …", "every weekday …", "check again in 30 min", or closing a fired cron job. Also use to reschedule or list jobs. NOT for one-off in-session waits. NEVER edit jobs.md by hand (the cron parser is strict).
---

# Jobs

Durable scheduled work lives in `~/.physiclaw/jobs/jobs.md`. The
format is regex-parsed by the cron loop — a malformed field breaks
every scheduled job in the file. **Never edit `jobs.md` directly.**

All job operations go through a single CLI:

```bash
uv run python "$SKILL_DIR/jobs.py" <subcommand> [args]
```

Four subcommands: `create`, `list`, `get`, `finish`. Each is a thin
wrapper around the canonical engine module — the file format stays
invariant no matter what you pass.

`$SKILL_DIR` is provided by Claude Code; it resolves to the directory
containing this SKILL.md.

---

## Create a job

```bash
uv run python "$SKILL_DIR/jobs.py" create \
  --id <user>-<topic>-<YYYY-MM-DD> \
  --kind <one-time|periodic> \
  --schedule "<5-field cron>" \
  --description "<human text>" \
  --context "<what the next wake needs to know>"
```

- `--kind` defaults to `one-time` if omitted.
- `--schedule`: standard 5-field cron `min hour dom mon dow`. Day-
  of-week is 0=Sun, 6=Sat. All times are naive local time. Examples:
  - Every weekday at 09:00 → `"0 9 * * 1-5"`
  - At 14:30 on 2026-05-01 → `"30 14 1 5 *"` (combined with a
    matching `one-time` kind)
  - In 30 minutes (compute wall-clock now + 30m yourself):
    `"M H D Mo *"` where M/H/D/Mo are the computed minute/hour/day/month.
- `--context`: at least 10 characters. Write what the next wake
  needs to do — the engine injects this into the SYSTEM prompt when
  the job fires.

**Id format:** `<user>-<topic>-<YYYY-MM-DD>`. Lowercase letters,
digits, hyphens only. The date suffix keeps repeat ids
(`alice-water-plants-…`) unique across days without `-v2` churn.

On duplicate id (even terminal), the command errors — pick a fresh id.

### Example

```bash
uv run python "$SKILL_DIR/jobs.py" create \
  --id alice-water-plants-2026-05-01 \
  --schedule "0 9 1 5 *" \
  --description "Remind Alice to water the kitchen plants." \
  --context "Send Alice a friendly reminder via WeChat to water the kitchen herbs."
```

---

## List jobs

```bash
uv run python "$SKILL_DIR/jobs.py" list [--status pend|fired|done|fail|cancel|all]
```

Default is `all`. Prints one job per line with id, status, kind,
schedule, next fire, and short description.

**Common recovery move:** start a wake with
`jobs.py list --status fired` to find orphaned fired jobs from earlier
wakes that you need to close.

---

## Get one job

```bash
uv run python "$SKILL_DIR/jobs.py" get --id <id>
```

Prints every field (description, type, status, schedule, context,
all timestamps, execution result). Use when the one-liner from
`list` isn't enough.

---

## Finish a fired job

```bash
uv run python "$SKILL_DIR/jobs.py" finish \
  --id <id> --status <done|fail|cancel> --recap "<one-line outcome>"
```

- **done** — work complete.
- **fail** — blocked or impossible.
- **cancel** — no longer needed, or you're rescheduling (do
  `cancel` + new `create` with a fresh id).

For **periodic** jobs, `done`/`fail` automatically reset to `pend`
so the next cycle fires. Only `cancel` is permanent for periodic.

Errors on already-terminal jobs (re-finishing is a bug, not
idempotent).

---

## Reschedule = cancel + create

There is no `update`. Jobs are append-only; their only state change
is finishing them. To reschedule:

1. `jobs.py finish --id <old> --status cancel --recap "rescheduling — new id <new>"`
2. `jobs.py create --id <new> …` (use a fresh id — bump the date, or
   append `-v2` if same-day)

---

## WAIT close pattern

When closing with `>> WAIT - <reason>`, pair it with a fresh job
for the resume check. Otherwise the engine auto-schedules a generic
15-min follow-up — usually wrong for your situation. Only fall back
to that if you genuinely don't know when to check back.

Example:

```bash
uv run python "$SKILL_DIR/jobs.py" create \
  --id alice-confirm-order-2026-05-01 \
  --schedule "$(date -v +30M '+%M %H %d %m') *" \
  --description "Re-check IM: user said they'd confirm in ~30min." \
  --context "Alice reviewed cart before leaving for a meeting; re-open WeChat and look for her OK to place the order."
```
