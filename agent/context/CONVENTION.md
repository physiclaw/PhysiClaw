# Convention

Use native tool_calls.

## Turn rules

- **Every turn is exactly two tool calls: `note` plus one other.** No
  more, no less. `note.summary` is one line saying what you're doing
  this turn and why. That single line is the permanent record: it
  survives compaction and labels any view image later dropped from
  history, so write it so a reader picking up cold still understands
  the move.
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
- Once you read the owner's message, call `update_progress(owner_said,
  understanding, steps)` to replace the seed with the real task. Every
  step is a `{content, status}` object; status is `pending`, `in_progress`,
  or `completed`. Exactly ONE step may be `in_progress` at a time.
- **Follow the plan step-by-step; tick when a step's INTENT is
  achieved, not after every tap.** A step is a logical intent (e.g.
  "Search chips and add to cart"), which typically spans 10–15
  tap+peek turns. Stay `in_progress` for that whole span, then the
  moment the screen confirms the intent (add-to-cart toast, count
  badge increments, etc.), call `[note, update_progress]` to flip the
  finished step to `completed` and the next step to `in_progress`.
  Without this tick, the plan goes stale and you risk re-doing a step
  you already finished (the JD double-add-to-cart pattern).
- Whenever the plan shifts otherwise (unexpected screen, owner adjusts,
  partial failure), call `update_progress` again. Only pass fields you
  want to change.

## Compaction: latest screen wins

Only the most recent `peek` / `screenshot` tool_result keeps its image
and full listing. Earlier view results are stubbed down to a marker
line (`(superseded <tool>)`) plus the **text-kind rows** from the
original listing. Icon rows are dropped — without the image, their
numbered boxes are opaque — but text rows stay re-targetable because
their label tells you what and where. The next turn's `note.summary`
already sits in that turn's assistant message immediately after the
stub, so the transcript reads naturally as "stub → what I did next"
without any duplicated prose. The assistant messages and `note`
tool_results stay intact; decision history is preserved.

Consequence: for a tap on a labelled target you've seen before (a
nav tab, a CTA like "加入购物车", a category name), the text row
survives compaction — you can reference it many turns later without
re-observing. For anything icon-only (app icons without a label, raw
thumbnails, detail-page controls that show only an icon), re-`peek`
when you need it.

## Bboxes come from the listing, never from eyeballing

Every physical-action bbox must be copied verbatim from a bbox in the
most recent `peek` / `screenshot` listing, or from a text row that
survived compaction in an earlier view's stub.

**Verbatim means character-for-character.** Transcribe the four numbers
*between the brackets* of the listing row you picked — do not retype
them from memory, do not re-read the digits off the image, do not
round or normalize. `0.520` stays `0.520` (not `0.52`, not `0.518`).
A one-digit drift sounds tiny but can land a tap on the neighboring
icon for small targets — and the model's natural tendency is to
regenerate digits rather than copy them, so this rule is a deliberate
correction, not a suggestion.

Procedure: find the target row in the listing, identify its bbox
brackets `[left,top,right,bottom]`, put exactly those four numbers
(same decimal digits, same order) into the bbox argument.

Never guess, never round, never average two listing rows, never
"eyeball" coordinates from the image. If the element you want isn't
in any current or surviving listing row, re-observe with a more
accurate view — `screenshot` > `peek` in fidelity. Step up the ladder
rather than re-running `peek` and hoping for a better listing.

This is what makes `sequence` safe: each step's bbox is grounded in
the listing that was live when you planned the chain. A made-up bbox
turns `sequence` into blind tapping and compounds errors step by step.

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
- **Wait for owner: short-wait first, escalate to WAIT only after
  retrying.** When you've sent the owner a message and need a reply,
  the pattern is: `wait(30-60)` → `peek` IM → if no reply, `wait`
  again (up to ~3 attempts, total ≤3 min). Only after that, give up
  the session and escalate: `end_session(WAIT, ...)` + `create_job`
  for a minutes/hours-scale resume. Short waits keep you in-flow if
  the owner is actively engaged; the cap on retries prevents holding
  the loop open when they've genuinely stepped away.

