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

Only the most recent `peek` / `screenshot` tool_result keeps its image
+ listing. Earlier view results are stubbed in place with
`(superseded <tool> — past view: <desc>)`, followed by a block of
pinned elements if the next turn's note carried any. Both `<desc>` and
the pinned block are pulled from the **next turn's** `note` (composed
while that image was still the latest view) — so always fill
`note.screen` on the turn right after a view tool runs, and fill
`note.key_ui_elements` if you need specific bboxes to survive past
this turn. The assistant messages and `note` tool_results stay intact;
the agent's decision history is preserved, only the bulky pixel
payload is elided.

Consequence: don't rely on a `peek` from three turns ago to plan the
current tap. If you need to re-check, re-observe — it's cheap. If you
need a specific bbox to survive, pin it in the next turn's
`note.key_ui_elements`.

## Bboxes come from the listing, never from eyeballing

Every physical-action bbox must be copied verbatim from a bbox in the
most recent `peek` / `screenshot` listing — or from a prior
`note.key_ui_elements` entry whose pinned screen is still current (see
next section).

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
"eyeball" coordinates from the image. If the element you want isn't in
the current listing and isn't pinned, re-observe with a more accurate
view — `screenshot` > `peek` in fidelity. Step up the ladder rather
than re-running `peek` and hoping for a better listing.

This is what makes `sequence` safe: each step's bbox is grounded in the
listing that was live when you planned the chain. A made-up bbox turns
`sequence` into blind tapping and compounds errors step by step.

## Pinning bboxes across compaction (`note.key_ui_elements`)

Compaction stubs every view tool_result except the latest. Pins live in
`note` args, which survive compaction forever — think of them as a
**working-set cache of actionable bboxes on the current screen type**
that you'll tap across many future turns.

### What to pin

Pin the call-to-action and navigation anchors that stay visible as
long as you're on (or returning to) this screen type. On a typical
list/search page that's something like:

- **CTAs on each row** — `+` add-to-cart buttons, order / buy / like /
  follow buttons, inline actions.
- **Navigation anchors** — the search box at top, the cart icon in
  the footer, the bottom-tab row (分类 / 购物车 / 我), the back arrow,
  visible category tabs you might switch to.
- **Product / row handles** — the tappable region of items you plan
  to open (image / title bbox for each row you'll click into).

On a detail page the working set is different: the primary CTA (加入
购物车, 立即购买), the back arrow, spec/variant selector, quantity
stepper.

The pinned set is the agent's reusable dispatch table for this screen.
You tap back and forth — open an item, come back, tap the next one,
tap `+` on a third — all using bboxes from the pin set. No re-
grounding per turn.

### What NOT to pin

- **The bbox you're tapping THIS turn.** It's already in the tap /
  swipe / long_press args; pinning is duplicate.
- **Pure decoration** — prices, timestamps, descriptive text next to
  a button. You won't tap them; they just bloat every future stub.
- **Elements on a different screen type.** A detail-page pin is stale
  the moment you return to the list; a home-screen pin is dead once
  you open an app. When the screen type changes, re-pin the new one's
  working set.

### Per-entry rules

- **Key** — slug-style handle (`add_to_cart`, `cart_icon`, `back`,
  `search_box`, `row_3_plus`). Lowercase + underscores. This is what
  you'll reference on later turns.
- **`kind`** — `"icon"` or `"text"`. Matches the listing row.
- **`label`** — YOUR reading of what the element is. Fill every entry
  (no empties). For text rows where the OCR is garbled, write the
  correct text. For icons, describe what it looks like
  (`"shopping cart"`, `"back arrow"`, `"+"`). Bad labels mean you'll
  pick the wrong bbox later.
- **`bbox`** — copied **character-for-character** from the listing
  row's bracket contents. Transcribe `[0.520,0.662,0.717,0.775]` as
  `[0.520, 0.662, 0.717, 0.775]` — exact same digits, no retyping from
  memory, no rounding `0.520` to `0.52` or drifting to `0.518`. Same
  rule as the top-level "Bboxes come from the listing" section — it
  applies here too, since pinned bboxes are read back later like live
  listing rows.


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

