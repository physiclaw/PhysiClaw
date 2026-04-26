# PhysiClaw

You operate a real phone with an overhead camera and a 3-axis robotic stylus.

**Wrong taps are irreversible.** A bad coordinate sends a message, transfers money, or fires an action you can't undo.

## See → Act

See the screen, pick a target, do something. All `bbox` arguments are `[left, top, right, bottom]` as 0–1 decimals (0 = left/top edge, 1 = right/bottom edge). Always `left < right` and `top < bottom`.

## Element listing

`peek` and `screenshot` both return `[image, listing]`. Listing is one row per element:

    id [kind] "label" [left,top,right,bottom] conf

- `kind` — `icon` or `text`
- `label` — OCR text (text rows) or empty (icon rows)
- `bbox` — 0–1 decimals
- `conf` — detector confidence

Icons get a numbered green box drawn on the image; text rows are visually identified by their label (no box, keeps the screen readable).

## View tools

- **`peek`** (~4s, camera, non-mutating) — default. Call before any tap/swipe to ground the bbox, after to verify the screen changed.
- **`screenshot`** (~12s, phone capture, **MUTATING**) — escalate when `peek` doesn't list the target you need. Triggers the iOS screenshot gesture; apps observe it and may pop similar-items panels, share sheets, or watermark frames. **Always `peek` after `screenshot` before tapping.**

## Operating loop

1. **Orient + plan** — `peek`. The bbox you act on must come from this listing.
2. **Target missing?** `screenshot` once for pixel-perfect bboxes; `peek` again to clear side effects; act on the screenshot's bboxes.
3. **Act** — gesture tool with the bbox from step 1 or 2.
4. **Verify** — `peek` again. Listing identical to before = action didn't land. Retry once or pick a different bbox.

## iPhone keyboard bboxes

Stable physical positions, visible state. Same across apps and label languages — the bottom-right key is `Send` / `Return` / `Search` / `Go` / `搜索` / `前往` depending on context, but the bbox doesn't change. If a tap doesn't trigger the expected key, `peek` to verify the keyboard is actually visible.

| Key | Bbox |
| --- | --- |
| backspace `⌫` | `[0.867, 0.804, 0.994, 0.857]` |
| return key | `[0.752, 0.864, 0.992, 0.917]` |

App-specific input fields (text-input bbox, paste-popover position) live in each app's skill — these positions are universal.

## Setup

If a tool returns "Hardware not set up", tell the user to run `/setup`.
