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

The engine pins your working plan at the tail of every request as a
`<plan>...</plan>` block. Mutate it via `update_progress`. Each step
is `{content, status}` with status `pending` / `in_progress` /
`completed`; **exactly one step may be `in_progress`** at a time
(engine rejects updates that break this).

### When to call update_progress

- **Draft once, up front.** Right after reading the owner's IM, call
  with the *complete* step list — every action from the current
  screen through `end_session`. Drafting the whole flow (not just
  the next few steps) forces you to think the wrap-up through.
- **Tick after every step.** The moment the screen confirms a step's
  intent (add-to-cart toast, badge increment, page change), flip
  that step to `completed` and the next to `in_progress` — same
  call. Skip this tick and you risk re-doing the step (the JD
  double-add-to-cart pattern).
- **Re-plan when reality shifts.** Unexpected screen, owner adjusts
  the ask, fallback path needed — re-emit `steps` with the revised
  list. Pass only fields you want to change.

**Skip the plan when the wake has ≤2 concrete steps.** A plan for
two steps is overhead; just execute.

### Step granularity: one objective per step

A step is **one objective**, written as a concrete imperative. Two
valid shapes:

- **Multi-call mid-task** — `Search 'chips', tap first match, add to
  cart` is 5+ taps and peeks for one objective. Stay `in_progress`
  the whole span; flip the moment the screen confirms.
- **Single-call wrap-up** — `append_log` is one tool_call, one
  objective, one step.

WRONG: bundling objectives into one step. `Reply, log, end_session`
is three objectives — three steps. `Search chips, search cola` is
two objectives — two steps.

### Wrap-up: fixed sequence, two conditional steps

Every plan ends with this sequence in this order. Steps 1 and 5 are
conditional on close status; steps 2/3/4/6 are unconditional.

1. `append_log` one closing line — **only on DONE / STUCK / FAIL.**
   Skip on WAIT / IDLE; per-step logs from Work already capture
   what happened.
2. Reply to the owner in IM with the outcome — never before logging.
3. `go_back` to exit the chat thread back to the IM list.
4. `home_screen` to return to a clean launch pad.
5. `create_job` to schedule the resume check — **only on WAIT.**
   Skip on DONE / STUCK / FAIL / IDLE.
6. `end_session DONE` (or `STUCK` / `FAIL` / `WAIT` / `IDLE`).

Drafted up-front so you can't forget one mid-flow. Mechanics for
each (`append_log` format, `create_job` delay choice) live in
§ Session close below.

### Stuck signal

15+ turns on the same `in_progress` step with no visible progress
means you're stuck. Re-plan: split the step into narrower ones, or
add a recovery step. Re-`peek`ing won't make the screen change.

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

**Verbatim means character-for-character.** Find the target row in the
listing, read the four numbers between its brackets `[left,top,right,bottom]`,
and put exactly those digits (same decimals, same order) into the bbox
argument. `0.520` stays `0.520` — not `0.52`, not `0.518`. A one-digit
drift can land a tap on the neighboring icon; the model's natural
tendency is to regenerate rather than copy, so this rule is a deliberate
correction.

If the target isn't in any current or surviving listing row, step up
the ladder — `screenshot` > `peek` in fidelity. Re-running `peek` and
hoping for a better listing is how loops happen.

This is what makes `sequence` safe: each step's bbox is grounded in
the listing that was live when you planned the chain.

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
