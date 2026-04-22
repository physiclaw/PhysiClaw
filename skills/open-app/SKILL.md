---
name: open-app
description: Use when you need to launch an app that is NOT on the current screen and NOT in the dock — any time "open <app>" is the next step and you can't see the icon. Use FIRST before tapping blindly. NOT needed when the target app's icon is already visible.
---

# Open App via Spotlight

**Argument:** App name (e.g., "美团", "WeChat", "Safari").

## Steps

Each step is one `[note, one-other]` turn. `<name>` placeholders
refer to the **Fixed elements** table at the bottom.

1. `send_to_clipboard(text="<app name>")`
   — copy the app name (use the exact text the owner asked for, e.g.
   `"美团"`, `"WeChat"`).
2. `home_screen()` — return to a clean launch pad. **Skip if you're
   already on the home screen** (peek/scan shows app-icon grid + dock,
   no in-app chrome) — slow physical motion (~2s); don't waste it.
3. `swipe(bbox=<spotlight-pull>, direction="down", size="l")`
   — open Spotlight. Bbox is mid-screen (NOT the top edge — that
   opens Notification Center). `size="l"` (~4cm) avoids overshoot.
4. `peek()` — refresh listing; the search field and keyboard should
   be visible.
5. **If the field has stale text** (no "搜索"/"Search" placeholder),
   clear it via tap+backspace. Chain with ONE `sequence` call (still
   one tool call, so the turn remains `[note, one-other]`):

   ```python
   sequence(
     step1={"tool_name": "tap", "arg": <search-field>},
     step2={"tool_name": "tap", "arg": <backspace>},
     step3={"tool_name": "tap", "arg": <backspace>},
     step4={"tool_name": "tap", "arg": <backspace>},
     step5={"tool_name": "tap", "arg": <backspace>},
   )
   ```
   Tap 1 focuses; taps 2-5 each delete one char. Over-tapping an
   empty field is a no-op — safe to over-estimate. `peek` to verify;
   if text remains, run another sequence. **Skip when the field is
   empty.** Prefer tap-backspace over `long_press(backspace)` —
   per-tap deletions are deterministic.
6. `long_press(bbox=<search-field>)` — opens the Paste popover above
   the search field. If no popover appears after long-press, you
   tapped the wrong element — re-`peek` and pick again.
7. `peek()` — refresh listing; "Paste" / "粘贴" appears in the popover.
8. `tap(bbox=<paste-button>)` — paste the app name into the field.
9. `peek()` — refresh listing; search results render below the field.
10. `tap(bbox=<app-icon>)` — launch the app. (See `<app-icon>` row
    below for decoy warnings.)

## Fixed elements

Typical bboxes as priors — per CONVENTION.md, copy verbatim from the
latest `peek` / `screenshot` listing before tapping.

| Name | Typical bbox | How to find it / decoys |
|---|---|---|
| `<spotlight-pull>` | `[0.3, 0.4, 0.7, 0.6]` | Mid-screen rectangle. Swipe-down anchor; not a peek target. |
| `<search-field>` | `[0.11, 0.60, 0.99, 0.66]` | The **focused input** at the BOTTOM, just above the keyboard, y≈0.62. Full-width field with mic icon on right. |
| `<backspace>` | see PHYSICLAW.md "iPhone keyboard bboxes" | — |
| `<paste-button>` | `[0.09, 0.56, 0.19, 0.58]` | Row labeled `Paste` / `粘贴` in the pill popover just ABOVE the focused field at y≈0.56. Often next to an `AutoFill` button (same y, x≈0.26-0.38) — pick `Paste`, not `AutoFill`. Dismisses if you tap elsewhere first. |
| `<app-icon>` | varies — in search results below the field | Row whose label matches the app name **exactly**. Skip rows with App Store badges or "in Safari" / web hits. |
