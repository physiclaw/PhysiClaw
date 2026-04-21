---
name: search-in-app
description: Use when typing a query into a search box / search field inside any app — JD, Taobao, Meituan, App Store, Spotlight, Settings, etc. Covers focus → clear stale text → paste → submit. NOT for sending IM messages (use the relevant chat skill).
---

# Search in App

Most apps with a search bar follow the same pattern: tap to focus, paste your query, hit return.

## Flow

1. `send_to_clipboard(text)` — your query.
2. `tap` the search field to focus it (bbox from a prior `scan` / `peek`); the keyboard appears.
3. **If the field has stale text from a previous search, tap `backspace` 10–20 times.** Pasting onto a non-empty field does not replace the existing text, so partial-clear leaves you searching for `"oldnew"` and matching nothing. Over-tapping is safe — extra presses on an empty field are no-ops, and you usually can't tell from a `scan` how many characters are in a focused field.
4. `long_press` the search field, then `tap` **Paste** in the popover.
5. `tap` the keyboard's return key (bbox in PHYSICLAW.md "iPhone keyboard bboxes").
6. Verify with `peek` or `scan` that the results page rendered.

## Common pitfalls

- **Auto-suggest dropdown**: typing or pasting may surface a list of suggestions. The keyboard return key submits **your typed string** — tap it instead of any suggestion unless you specifically want a suggestion.
- **Camera misses the keyboard**: `peek` of the keyboard area can be glare-prone. If a `tap` on backspace seems to do nothing for several attempts, `screenshot` once to confirm the keyboard is actually visible (it may have hidden, or the field may have lost focus).
- **Tap didn't open the keyboard**: the field may already be focused (cursor visible, no keyboard) — tap once more, or tap a non-field area first then re-tap the search field to reset.
