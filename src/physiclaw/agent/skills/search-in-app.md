---
name: search-in-app
description: Use when typing a query into a search box / search field inside any app — JD, Taobao, Meituan, App Store, Spotlight, Settings, etc. Covers focus → clear stale text → paste → submit. NOT for sending IM messages (use the `im` skill).
---

# Search in App

Generic pattern: focus, paste, return.

1. `send_to_clipboard(text)`.
2. `tap` the search field (bbox from the latest `peek`); keyboard appears.
3. **Stale text?** Tap `<backspace>` 10–20× (one `sequence`). Paste does NOT replace existing text — a partial clear leaves you searching `"oldnew"`. Over-tapping is safe.
4. `long_press` the search field — **its own turn**: the popover's bbox doesn't exist until the long_press fires (CONVENTION § Sequence bundling).
5. `peek` — find the actual Paste popover bbox.
6. One `sequence`: `tap` Paste (bbox from step 5) + `tap` return (learned keyboard layout) — both grounded.
7. `peek` — results page rendered.

## Pitfalls

- **Auto-suggest dropdown** — return submits **your typed string**; tap a suggestion only if you specifically want it.
- **Placeholder, not stale text** — gray hint text isn't deletable; it vanishes on paste. Backspace changing nothing → just paste.
- **Tap didn't raise the keyboard** — field may already be focused (cursor, no keyboard). Tap once more, or tap elsewhere then re-tap.
