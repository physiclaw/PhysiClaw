---
name: search-in-app
description: Use when typing a query into a search box / search field inside any app — JD, Taobao, Meituan, App Store, Spotlight, Settings, etc. Covers focus → clear stale text → paste → submit. NOT for sending IM messages (use the relevant chat skill).
---

# Search in App

Most apps with a search bar follow the same pattern: tap to focus, paste, hit return.

## Flow

1. `send_to_clipboard(text)` — your query.
2. `tap` the search field to focus it (bbox from a prior `peek`); the keyboard appears.
3. **Stale text?** Tap `backspace` 10–20 times. Pasting onto a non-empty field does NOT replace existing text — partial-clear leaves you searching for `"oldnew"` and matching nothing. Over-tapping is safe; you usually can't tell from `peek` how many characters are in a focused field.
4. `long_press` the search field. **Its own turn** — the Paste popover doesn't exist until `long_press` fires, so it can't share a `sequence` with the tap that triggers it.
5. Next turn: `peek` to find the actual Paste popover bbox (depends on where you long-pressed).
6. `tap` the Paste button using the bbox from step 5.
7. `tap` the keyboard's return key (PHYSICLAW § iPhone keyboard bboxes).
8. `peek` to verify the results page rendered.

### What `sequence` can bundle

- ✅ **N×backspace** in one sequence (5–20 taps on the same fixed bbox).
- ❌ `long_press` + `tap_Paste` in this generic flow — the Paste popover bbox is born from the long_press itself, so bundling means tapping a guessed bbox, violating CONVENTION § Bboxes. Two turns.
- ✅ **After Paste is located** (step 5 done): `tap_Paste` + `tap_Return` — both bboxes are grounded (Paste from step 5's peek, Return from PHYSICLAW).

App-specific skills with a **pinned input bbox** (e.g. wechat fast-path) can bundle `long_press + tap_Paste` because their Paste popover position is predictable. That's a property of those apps, not a generic move.

## Pitfalls

- **Auto-suggest dropdown** — typing surfaces a list of suggestions. Return submits **your typed string**; tap it instead of any suggestion unless you specifically want one.
- **Camera misses the keyboard** — keyboard-area `peek` can be glare-prone. Backspace tap doing nothing for several attempts → `screenshot` once to confirm the keyboard is actually visible.
- **Tap didn't open the keyboard** — field may already be focused (cursor visible, no keyboard). Tap once more, or tap a non-field area first then re-tap to reset.
