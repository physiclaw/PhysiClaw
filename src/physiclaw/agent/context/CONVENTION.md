# Convention

Act through native tool_calls — never write calls as prose text.

## Turn rules

- **Every turn = `[note, one-other]`** — exactly two tool calls. Zero or text-only calls stall the loop.
- `note.summary`: one line, ≤20 words — the only part of an aged-out turn that survives compaction (§ Compaction). Write it for a cold reader.
- **Admin splits across turns**: `append_log` → `end_session`; `save_memory` → `append_log` → `end_session`.

## The plan

Pinned at the request tail as `<plan>`; mutate via `update_progress`. Steps are `{content, status}` (`pending` / `in_progress` / `completed`); **exactly one `in_progress`** (engine-enforced). Skip the plan when the wake has ≤2 concrete steps.

- **Draft once, up front** — right after reading the IM, full list through `end_session`.
- **Tick on intent-confirmed** — cart toast, badge increment, page change → flip `completed` → next `in_progress` in the same call. Skipping risks re-doing steps.
- **Re-plan on shift** — unexpected screen, changed ask, fallback path → re-emit `steps`; pass only changed fields.
- **One objective per step**, concrete imperative. `Search 'chips', tap first match, add to cart` = one step spanning 5+ calls; `append_log` = one single-call step. Don't bundle objectives: `Reply, log, end_session` is three steps; `Search chips, search cola` is two.

## Compaction

Automatic, two layers:

- **Per-turn: latest screen wins.** Only the newest `peek`/`screenshot` keeps its image + full listing; earlier ones stub to `(superseded <tool>)` plus **text rows**. Text rows (labelled buttons, tabs, category names — `Add to Cart`) stay re-targetable; icon rows drop — re-`peek`.
- **Turn-age (~30 turns).** Older turns fold into pinned slots: `[earlier turns]` (one bullet per turn = its `note.summary`; the rest is gone), `[memory loads]` (all `read_memory`/`read_logs` results, in full), `[loaded skills]` (all skill bodies, in full). The last ~10 turns stay intact; plan + scratchpad sit at the tail.

→ Never rely on an old screen — only its `note.summary` bullet survives. DO trust already-loaded skills and logs — never reload. Anything bigger than a one-liner → scratchpad.

## Scratchpad

Free-form working memory, rendered as `<scratchpad>` at the request tail; **survives compaction**. Accumulate everything that feeds the answer — order details, prices, addresses, a draft reply, a bbox to carry past a superseding peek (§ Bboxes) — so the final reply pastes from it. Write via `note(summary=..., scratchpad=...)`; reissue the full text to extend, empty string to clear. Plan = what to do next; scratchpad = what you've gathered.

## Bboxes — copy verbatim, never eyeball

Every action bbox comes verbatim — every digit — from a grounded source: the latest `peek`/`screenshot` listing, a surviving text row from an earlier stub, a scratchpad copy of a prior listing row, or **SYSTEM § Screen layout**. Target absent from all → escalate to `screenshot`; never fabricate coords. Sole exception: an element-free target (empty area to dismiss, a swipe anchor) may be estimated.

## Sequence bundling

`sequence` (≤5 actions) is safe only when every step's bbox is grounded at planning time. A Paste popover born from a `long_press` *inside* the sequence is NOT grounded — give the `long_press` its own turn, `peek` the popover, then tap. **Exception:** inputs pinned in SYSTEM § Screen layout (chat input, Spotlight) have learned Paste boxes, so `long_press + tap Paste` may bundle there.

## Stuck

10+ turns on one `in_progress` step with no visible progress = stuck. Escalate in order:

1. **Re-plan** — split the step or add a recovery step.
2. **Back out** — `go_back` to the app's home, re-pick the entry.
3. **Force-quit + reopen** — for popups that won't dismiss, looping back stacks, the wrong page returning.

## Wait-retry for user replies

`wait(30–60)` → `peek` IM → no reply → repeat. **Max 3 attempts, ≤3 min total.** Then `create_job` a minutes/hours-scale resume and close WAIT (AGENT § Close).
