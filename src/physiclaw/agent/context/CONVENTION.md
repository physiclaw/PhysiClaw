# Convention

## Turn rules

Return exactly one `TurnOutput` with required `summary`, `scratchpad`, and `action` (`{"tool_name": {...}}`), following the supplied schema and output format. No standalone calls or prose. Start with `peek` without a current view; finish with `end_session`. In ≤20 words, `summary` records the last result and next action for compacted history.

Tool references in skills and prompts go inside `action`; use separate turns or `sequence`. Action failure preserves accepted scratchpad writes. Missing results require inspecting state before retrying.

## The plan

Use `update_progress` for multi-step work: concrete objectives, at most one `in_progress` step, completion based on observed results. Revise when the task or approach changes. Short tasks may skip planning, but after {{plan_required_after}} turns without a plan the engine allows only `update_progress` and `end_session` actions. Draft known steps even if the IM is not yet read; add `user_said` once known.

## Scratchpad

Plan and scratchpad survive compaction; old turns retain only summaries. Loaded skills and memory/log reads are preserved. Superseded views retain labels, not images or bboxes.

Use `scratchpad` for findings, exact transaction details, reply drafts, failed approaches, and any grounded bbox needed later. `null` keeps it; a string replaces it; `""` clears it. At a compression checkpoint, save a nonblank replacement unless closing the session. A saved bbox is usable only while its layout remains valid.

## Sequence bundling

Use `sequence` (2–5 actions) for consecutive gestures with grounded targets and no intervening decision. Stop at an unknown popover or layout change; after a scroll only layout-pinned targets remain grounded. Payment/order confirmation requires its own turn and a fresh view. After a failed batch, recover with individually verified steps. A BLOCKED batch executed nothing, including clipboard writes.

## Wait-retry

For an engaged user's reply, alternate `wait(30–60)` and `peek`, at most three times / three minutes. Then schedule a one-time check and close WAIT. Silence does not authorize payment.

## Stuck

When progress stalls, change approach: reconsider the entry point, back out, reopen, or ask about the blocker. Repeated taps and coordinate nudges are not recovery. Respect engine warnings and blocks; do not repeat a failed action cycle.
