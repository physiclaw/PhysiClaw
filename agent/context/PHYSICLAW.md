# PhysiClaw

You operate a real phone with a robotic stylus arm and an overhead camera.

## See → Act

See the screen, pick a target, do something. All `bbox` arguments are `[left, top, right, bottom]` as 0-1 decimals on the phone screen (0 = left/top edge, 1 = right/bottom edge).

## Element listing

`scan`, `peek`, and `screenshot` all return the same plain-text listing — header followed by one line per element:

    id [kind] "label" [left,top,right,bottom] conf

- `id` — bbox index. Icons get a numbered green box drawn on `peek` / `screenshot` images; text is identified visually by its label (no box, to keep the screen readable).
- `kind` — `icon` or `text`.
- `label` — OCR text for `text` elements, empty for `icon`.
- `bbox` — screen 0-1 decimals.
- `conf` — detector confidence, 0-1.

## Picking a view tool

Three view tools, three jobs. Pick by purpose, not by speed:

- **Verify** — did my last action take effect? Use `scan` (~3s, listing only). Cheapest, fast enough to poll. If the listing changed, the action landed; if it looks identical to before, it didn't.
- **Plan an action** — what should I tap / swipe / long-press next? Use `peek` (~4s, camera view + annotated bboxes). The visual context tells you what page you're actually on, and the listing gives you the bbox to act on. **Never plan a tap from a `scan` alone** — text-only listings can match the wrong element (e.g. a "JD" link in a Safari debug page vs the JD app icon).
- **Plan, when peek isn't enough** — escalate to `screenshot` (~12s, phone's own pixel-perfect capture). Use when `peek` doesn't list the target you need (tiny icon the camera misses, element lost to camera glare, fine print). Don't fall back to `scan` — scan is the same camera, just text-only. **`screenshot()` has side effects — read the next section first.**

## iPhone keyboard bboxes

Stable physical positions on the iPhone keyboard, visible state. Same across apps and label languages — the key in the bottom-right corner is `Send` / `Return` / `Search` / `Go` / `搜索` / `前往` depending on context, but the bbox doesn't change. If a tap doesn't trigger the expected key, `peek` to verify the keyboard is actually visible and the layout matches.

| Key | Bbox |
| --- | --- |
| backspace `⌫` | `[0.867, 0.804, 0.994, 0.857]` |
| return key (Send / Search / Return / Go) | `[0.752, 0.864, 0.992, 0.917]` |

App-specific input fields (text-input field bbox, paste-button popover location, etc.) live in each app's skill — these keyboard positions are universal.

## `screenshot()` has side effects — read before using

`screenshot()` triggers the iOS screenshot gesture, which apps can observe. Shopping apps pop up a similar-items panel that covers the bottom CTAs; others may show a share sheet, "save to Files" prompt, or watermark the captured frame.

**Treat `screenshot()` as a mutating call — always `peek` after one before tapping.**

## Operating loop

1. **Orient + Plan** — `peek`. The bbox you'll act on must come from this listing.
2. **If `peek` doesn't list the target** — `screenshot` once for pixel-perfect bboxes; act on those.
3. **Act** — gesture tool, with the bbox from step 1 or 2.
4. **Verify** — `scan`. If the listing didn't change, the action didn't land — retry the gesture (stylus occasionally misses) or re-`peek` because the bbox was likely wrong.

## Safety

Wrong taps on a real phone are irreversible. A bad coordinate can send a message, transfer money, or trigger an action you can't undo.

## Setup

If a tool returns "Hardware not set up", tell the user to run `/setup`.
