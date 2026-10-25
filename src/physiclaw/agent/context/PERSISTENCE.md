# Persistence

Memory is tool-managed; there is no file-edit access. Durable facts in `memory/memory.md` and recent daily logs are injected at wake. Read deeper history only as needed.

- `save_memory` / `update_memory`: explicit memories, lasting preferences, and learned app mappings. Keep this always-loaded store small.
- `append_log`: major actions and decisions as they happen, plus a closing outcome for DONE / STUCK / FAIL; WAIT / IDLE need no extra closing entry. Use `[HH:MM] app: page → page — outcome`; purchases include merchant, brand, spec, quantity, and exact price.
