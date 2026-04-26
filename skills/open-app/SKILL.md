---
name: open-app
description: Use when you need to launch an app that is NOT on the current screen and NOT in the dock — any time "open <app>" is the next step and you can't see the icon. Use FIRST before tapping blindly. NOT needed when the target app's icon is already visible.
---

# Open App via Spotlight

**Argument:** app name (e.g. `美团`, `WeChat`, `Safari`).

Each step is one `[note, one-other]` turn. Placeholders refer to **Fixed elements** below.

## Steps

1. `send_to_clipboard(text="<app name>")` — use the exact text the user asked for.
2. `home_screen()` — return to a clean launch pad. **Skip if already on the home screen** (peek shows app-icon grid + dock, no in-app chrome) — physical motion is ~2s; don't waste it.
3. `swipe(bbox=<spotlight-pull>, direction="down", size="l")` — open Spotlight. Mid-screen origin (the top edge opens Notification Center instead). `size="l"` (~4cm) avoids overshoot.
4. `peek()` — search field + keyboard should be visible.
5. **Stale text?** Clear via tap + backspace, all in one `sequence` (one tool call):

   ```python
   sequence(
     step1={"tool_name": "tap", "arg": <search-field>},
     step2={"tool_name": "tap", "arg": <backspace>},
     step3={"tool_name": "tap", "arg": <backspace>},
     step4={"tool_name": "tap", "arg": <backspace>},
     step5={"tool_name": "tap", "arg": <backspace>},
   )
   ```

   Tap 1 focuses; taps 2–5 each delete one char. Over-tapping an empty field is a no-op — safe to over-estimate. Skip when the field is empty. Prefer tap-backspace over `long_press(backspace)` — per-tap deletions are deterministic.
6. `long_press(bbox=<search-field>)` — opens the Paste popover above the field. No popover = wrong element; re-`peek` and pick again.
7. `peek()` — `Paste` / `粘贴` appears in the popover.
8. `tap(bbox=<paste-button>)` — pastes the app name.
9. `peek()` — search results render below the field.
10. `tap(bbox=<app-icon>)` — launch.

## Fixed elements

Typical bboxes as priors — per CONVENTION § Bboxes, copy verbatim from the latest `peek` / `screenshot` listing before tapping.

| Name | Typical bbox | How to find / decoys |
|---|---|---|
| `<spotlight-pull>` | `[0.3, 0.4, 0.7, 0.6]` | Mid-screen rectangle. Swipe-down anchor; not a peek target. |
| `<search-field>` | `[0.11, 0.60, 0.99, 0.66]` | Focused input at the BOTTOM (y≈0.62), full-width, mic icon on right. |
| `<backspace>` | see PHYSICLAW § iPhone keyboard bboxes | — |
| `<paste-button>` | `[0.09, 0.56, 0.19, 0.58]` | `Paste` / `粘贴` row in the pill popover ABOVE the field at y≈0.56. Often next to `AutoFill` (same y, x≈0.26–0.38) — pick `Paste`, NOT `AutoFill`. Dismisses if you tap elsewhere first. |
| `<app-icon>` | varies — in search results | Row whose label matches **exactly**. Skip rows with App Store badges or "in Safari" / web hits. |
