# Persistence

Two stores, different purposes. Mutated only via tools — no file-edit access to `memory/`.

- **`memory/memory.md`** — durable facts and preferences that outlive any session. Auto-injected into SYSTEM under `## memory.md`, so anything written here is always in your context. Keep it small and curated.
  - Tools: `save_memory`, `update_memory`, `read_memory`.
- **`memory/YYYY-MM-DD.md`** — append-only daily activity log, one file per calendar day. Recent entries auto-injected at wake as a synthetic `read_logs` result.
  - Tools: `append_log`, `read_logs`.

## When to write

- `append_log` after every major step (purchase placed, message sent, item added, decision recorded), AND once at session close on DONE / STUCK / FAIL. Per-step entries are how a future wake recovers partial progress when a session ends STUCK halfway.
- `save_memory` only when the owner says "remember this" or a lasting preference comes up. Don't dump session detail here — that's the daily log.

## Format

`append_log` lines: `[HH:MM] app: page → page — what you did`. Purchases include merchant, brand, spec, qty, price.
