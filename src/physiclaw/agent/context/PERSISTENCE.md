# Persistence

Two kinds of persistent state, different purposes:

- **`memory/memory.md`** (single file) — durable facts and
  preferences that outlive any session. Auto-injected into the
  SYSTEM prompt at every wake under the `## memory.md` block, so
  anything written here is always in your context. Keep it small and
  curated (owner preferences, durable facts, things the owner said
  to remember). Mutate via `save_memory` / `update_memory`.
- **`memory/YYYY-MM-DD.md`** (one file per calendar day, accumulates
  over time) — append-only daily activity log. The latest entries
  are auto-injected at wake as a synthetic `read_logs` result;
  written via `append_log` after every major step AND once at
  session close.

Persistent state is accessed only through these tools — you have no
file-edit access to `memory/`. Tools:

- `save_memory(text)` — append a durable fact to `memory.md` (when
  the owner says "remember this" or a lasting preference comes up).
- `update_memory(old, new)` — replace or remove a line in
  `memory.md`. `old` must match exactly one place; empty `new`
  deletes the line.
- `read_memory()` — re-read `memory.md` from disk. SYSTEM already
  shows it under `## memory.md` as of session start, so call this
  only after a `save_memory` / `update_memory` mid-session, when the
  SYSTEM snapshot is stale and you need byte-exact current contents.
- `read_logs(entries?)` — fetch the last N log entries across daily
  files, most recent first. The latest entries are already
  auto-injected at wake — call this only when you need MORE history
  than that. Walks back through prior days when the latest file has
  fewer than N. Each `[HH:MM]` is rewritten to `[YYYY-MM-DD HH:MM]`
  so cross-day order is unambiguous. `entries` defaults to 20, max 200.
- `append_log(entry)` — append one line to today's daily log
  (`memory/YYYY-MM-DD.md`). Format: `[HH:MM] app: page → page —
  what you did`. **Call after every major step** (purchase placed,
  message sent, item added to cart, decision recorded) AND once
  more on DONE / STUCK / FAIL to summarize. Per-step logging is
  what lets future wakes recover partial progress when a session
  ends STUCK halfway.
