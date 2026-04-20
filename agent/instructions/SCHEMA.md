# Response format

JSON-object response schema for runtimes without native tool-calling.
Superseded by `CONVENTION.md` when tool-calling is available; overrides
AGENT.md's "Response format" section.

Reply with EXACTLY one JSON object, no prose, no markdown fence. Fields:

```text
{
  "thought":       string   — short private reasoning, <= 200 chars
  "description":   string   — what the CURRENT screen shows, 2-4 sentences.
                              Required whenever your last tool call was a view
                              tool. Empty "" when no current image is present.
  "curated_bbox":  array    — subset of the input bbox list worth keeping for
                              later turns. Each item: {id, role, label, bbox}
                              with bbox = [x1, y1, x2, y2] copied verbatim from
                              the input. Drop noise / status bars / decoration.
                              Empty [] allowed.
  "tool_calls":    array    — [{name, args}] — tools to run, in order.
                              Empty [] only when emitting a sentinel.
  "log_entry":     string|null — "[HH:MM] app: page → page — what you did".
                                 Emit on DONE / STUCK / FAIL. Null otherwise.
  "memory_save":   string|null — a durable fact to append to memory.md. Only
                                 when the owner says "remember this" or you
                                 learned a lasting preference. Null otherwise.
  "create_cron":   object|null — schedule a follow-up job (use for WAIT):
                                 {id, description, schedule, context, kind}
                                 kind: "one-time" (default) or "periodic".
                                 schedule: 5-field cron (min hour dom mon dow).
                                 id: lowercase letters/digits/hyphens only.
                                 context: at least 10 chars.
  "sentinel":      string|null — ">> DONE - recap" | ">> STUCK - blocker"
                                 | ">> FAIL - reason" | ">> IDLE - why"
                                 | ">> WAIT - what you're waiting on".
                                 Null while still working.
}
```

## Sentinel rules

- DONE  — task complete, result verified on screen. Emit log_entry.
- STUCK — cannot proceed; blocker is external (locked phone, no network, …).
- FAIL  — attempted and failed; the task cannot succeed as specified.
- IDLE  — wake happened but no work was needed (no new IM, no due cron).
- WAIT  — paused for an owner reply. Emit create_cron to resume, or a
          15-minute follow-up is scheduled automatically.

## Tool-call rules

- Each call: `{"name": "<tool>", "args": {<arg>: <value>, ...}}`
- View tools (`scan` / `peek` / `screenshot`) refresh your "current" view.
- Action tools (`tap` / `swipe` / `long_press` / `send_to_clipboard` /
  `home_screen` / `go_back`) do NOT refresh the view. Their text result
  is appended to history; your "current" image stays stale until you
  call a view tool.

## Curation discipline

You NEVER invent or modify coordinates. `curated_bbox` items must only
reference ids present in the latest input bbox list, copying their bbox
arrays verbatim. Only the label may be corrected (e.g. fixing OCR
garbage or naming an icon).
