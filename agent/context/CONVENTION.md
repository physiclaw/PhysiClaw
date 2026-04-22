# Convention

Use native tool_calls.

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
- **Track progress in `steps` itself.** After finishing each step, call
  `update_plan(steps=[...])` and rewrite the list — prefix the done
  step with `✓` (e.g. `✓ <step description> (done)`). The plan tail
  always reflects your true position; you don't need to scan history
  to remember what's done. Without `✓` markers, every wake-tail looks
  identical and you risk re-doing a step you already finished
  (the JD double-add-to-cart pattern).
- Whenever the plan shifts otherwise (unexpected screen, owner adjusts,
  partial failure), call `update_plan` again. Only pass fields you want
  to change.

## Compaction: latest screen wins

Only the most recent `scan` / `peek` / `screenshot` tool_result keeps
its image + listing. Earlier view results are stubbed in place with
`(superseded <tool> — past view: <desc>)`. The `<desc>` is pulled from
the **next turn's** `note.screen`, composed while that image was still
the latest view — so always fill `note.screen` on the turn right after
a view tool runs, or the stub loses its description. The assistant
messages and `note` tool_results stay intact; the agent's decision
history is preserved, only the bulky pixel payload is elided.

Consequence: don't rely on a `peek` from three turns ago to plan the
current tap. If you need to re-check, re-observe — it's cheap.

## Bboxes come from the listing, never from eyeballing

Every physical-action bbox must be copied verbatim from a bbox in the
most recent `scan` / `peek` / `screenshot` listing. Never guess, never
round, never average two listing rows, never "eyeball" coordinates from
an image. If the element you want isn't in the current listing,
re-observe with a more accurate view — `screenshot` > `peek` > `scan`
in fidelity. Step up the ladder rather than re-running the same tool
and hoping for a better listing.

This is what makes `sequence` safe: each step's bbox is grounded in the
listing that was live when you planned the chain. A made-up bbox turns
`sequence` into blind tapping and compounds errors step by step.

## Session close

Close with `end_session(status, recap)` where status is one of
DONE / STUCK / FAIL / IDLE / WAIT.

- On DONE / STUCK / FAIL, call `append_log(entry)` with one line in
  the form `[HH:MM] app: page → page — what you did` summarizing the
  close. This is in addition to the per-step `append_log`s you've
  already written during Work — see PERSISTENCE.md.
- **Exit the way you came in, then head home.** Before `end_session`,
  first `go_back()` out of the current thread / detail view to the
  parent list, then `home_screen()`. Two steps: the `go_back` clears the
  deep context; the `home_screen` lands on a known launch pad. Skip
  either and the next wake wastes turns re-orienting.
- On WAIT, **always** call `create_job(...)` to schedule the resume
  check — pick the right delay for what you're waiting on. If you skip
  it, the engine reschedules a single canonical job
  (`wait-check-auto`) for 15 minutes from now. That entry is reused
  across sessions (no `wait-check-<sid>` accumulation), and the
  generic delay is usually wrong (too soon for "delivery in 2h", too
  late for "owner replying now"). The auto-schedule is a safety net,
  not the default. See JOBS.md for the full job model.

