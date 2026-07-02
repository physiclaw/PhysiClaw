---
name: open-app
description: Use when you need to launch an app that is NOT on the current screen and NOT in the dock — any time "open <app>" is the next step and you can't see the icon. Use FIRST before tapping blindly. NOT needed when the target app's icon is already visible.
---

# Open App via Spotlight

**Argument:** app name (e.g. `美团`, `WhatsApp`, `Safari`). Each step is one `[note, one-other]` turn; placeholders → **Fixed elements**.

1. `send_to_clipboard(text="<app name>")` — the exact name the user used; a Chinese app keeps its Chinese name.
2. `home_screen` — **skip if already on the home screen** (icon grid + dock, no in-app chrome).
3. `swipe(bbox=<spotlight-pull>, direction="down", size="l")` — mid-screen origin (top edge opens Notification Center); `size="l"` avoids overshoot.
4. `peek` — search field + keyboard visible.
5. **Stale text?** One `sequence`: tap `<search-field>` then 4× tap `<backspace>`. Over-tapping an empty field is a no-op — over-estimate freely. Skip when empty. Prefer tap-backspace over `long_press(backspace)` — deterministic per tap.
6. `long_press(<search-field>)` + `tap(<paste-button>)` — bundle into one `sequence` (both learned; CONVENTION § Sequence bundling). No popover = wrong element; re-`peek`.
7. `peek` — results render below the field.
8. `tap(<app-icon>)` — launch.

## Fixed elements

`<search-field>`, `<backspace>`, `<paste-button>` resolve from SYSTEM § Screen layout. Also:

- `<spotlight-pull>` — `[0.3, 0.4, 0.7, 0.6]` prior; mid-screen swipe anchor, not a peek target.
- `<paste-button>` decoy — pick `Paste` / `粘贴` in the pill popover ABOVE the field, NOT `AutoFill`; it dismisses if you tap elsewhere first.
- `<app-icon>` — from the search results in the latest listing (§ Bboxes); label matches **exactly** — skip App Store badges and "in Safari" / web hits.
