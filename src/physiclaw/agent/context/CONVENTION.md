# Convention

Use native tool_calls.

## Turn rules

- **Every turn = `[note, one-other]`** — exactly two tool calls. `note.summary` is one line, ≤20 words. **It is the ONLY part of the turn that survives compaction** (see § Compaction) — write it so a reader picking up cold understands the move.
- **Split admin across turns.** `append_log` → `end_session`. `save_memory` → `append_log` → `end_session`. One step per turn.
- **Zero or text-only tool_calls stalls the loop.** Always emit `[note, one-other]` or `[note, end_session]`.

## The plan

The engine pins your plan at the tail of every request as `<plan>...</plan>`. Mutate via `update_progress`. Each step is `{content, status}` with status `pending` / `in_progress` / `completed`. **Exactly one step may be `in_progress`** (engine rejects violators).

**Skip the plan when the wake has ≤2 concrete steps.**

- **Draft once, up front** — right after reading the IM, full step list through `end_session`.
- **Tick after every step** — the moment the screen confirms intent (cart toast, badge increment, page change), flip `completed` → next `in_progress` in the same call. Skip → risk re-doing the step (JD double-add pattern).
- **Re-plan on shift** — unexpected screen, user adjusts the ask, fallback path needed → re-emit `steps`. Pass only changed fields.

**One objective per step, concrete imperative.** Two shapes:

- **Multi-call mid-task** — `Search 'chips', tap first match, add to cart` is 5+ tool calls for one objective. Stay `in_progress` the whole span.
- **Single-call wrap-up** — `append_log` is one tool_call, one step.

Wrong: bundling. `Reply, log, end_session` is three steps. `Search chips, search cola` is two.

## Compaction

Two layers, both automatic.

**Per-turn: latest screen wins.** Only the most recent `peek` / `screenshot` keeps its image and full listing. Earlier view results stub down to `(superseded <tool>)` plus the **text-kind rows**. Icon rows drop (numbered boxes are opaque without the image); text rows stay re-targetable via their label. Decision history (assistant messages, `note` results) is preserved.

→ Labelled targets (`加入购物车`, nav tabs, category names) survive — reference them many turns later. Icon-only targets — re-`peek`.

**Turn-age: collapse old turns into pinned slots.** After ~30 turns, older turns fold into three pinned slots near the top of the transcript. The most recent ~10 turns stay intact; the plan and the scratchpad (§ Scratchpad) sit at the tail.

- `[earlier turns]` — one bullet per old turn, taken from that turn's `note.summary`. The full turn (tool_calls, results, taps) is gone — only the bullet remains.
- `[memory loads]` — every prior `read_memory` / `read_logs` result, in full. Pinned because reloading defeats the load.
- `[loaded skills]` — every prior `Skill(...)` body and reference, in full. Pinned for the same reason.

→ Don't try to "remember" a 25-turn-old screen — only your `note.summary` bullet survives. DO trust skill bodies and `read_logs` results loaded earlier are still in context — don't reload. For payloads bigger than a one-line summary, use the scratchpad (§ Scratchpad).

## Scratchpad

Your free-form working memory — rendered as a `<scratchpad>...</scratchpad>` block at the request tail. **Survives compaction.** Use it for derived data that won't fit a `note.summary`: a list scraped from one app to send via another, intermediate counts, a draft reply you're refining over multiple turns.

The plan is for *what to do next*; the scratchpad is for *what you've found out*.

Write API and replace-verbatim semantics: see the `scratchpad` tool.

## Bboxes — copy verbatim, never eyeball

Every physical-action bbox must come verbatim from the most recent `peek` / `screenshot` listing, or a text row that survived compaction in an earlier stub.

**Verbatim copy — every digit, every decimal.** `0.520` stays `0.520`, not `0.52`, not `0.518`. The model's instinct is to regenerate rather than copy; a one-digit drift lands on the neighboring icon.

Target missing from current and surviving listings? Step up: `screenshot` > `peek` in fidelity. Re-running `peek` hoping for a better listing is how loops happen.

This is what makes `sequence` safe — each step's bbox is grounded in the listing live when the chain was planned.

## Stuck

**10+ turns on the same `in_progress` step with no visible progress = stuck.**

1. **Re-plan** — split the step or add a recovery step.
2. **Back out** — `go_back` to the app's top, re-pick the entry.
3. **Force-quit + reopen** — `force_quit` resets app state, then reopen the app fresh. Use when popups won't dismiss, the back stack loops, or the wrong page keeps returning.

## Wait-retry for user replies

Pattern: `wait(30-60)` → `peek` IM → no reply → `wait` again. **Max 3 attempts, ≤3 min total.** After that, escalate: `end_session(WAIT, ...)` + `create_job` for a minutes/hours-scale resume. Short waits keep you in-flow if the user is engaged; the cap prevents holding the loop open when they've stepped away.

## Session close

See AGENT § Loop, **Close** phase.
