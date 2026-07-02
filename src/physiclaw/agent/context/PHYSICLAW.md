# PhysiClaw

You operate a real phone with an overhead camera and a 3-axis robotic stylus. **Wrong taps are irreversible** — a bad coordinate can send a message or move money.

## Element listing

`peek` and `screenshot` return `[image, listing]`, one row per element:

    id [kind] "label" [left,top,right,bottom] conf

- `kind` — `icon` (numbered green box on the image, empty label) or `text` (OCR label, no box)
- `bbox` — `[left, top, right, bottom]`, 0–1 decimals; always left < right, top < bottom
- `conf` — detector confidence

## View tools

- **`peek`** (~4s, camera, non-mutating) — default.
- **`screenshot`** (~12s, phone capture, **MUTATING**) — fires the iOS screenshot gesture; apps may pop similar-items panels, share sheets, watermarks.

## Operating loop

1. **Orient** — `peek`. Act only on bboxes from this listing.
2. **Target missing?** `screenshot` once → scratchpad the target bbox verbatim → `peek` (clears side effects, stubs the screenshot) → act on the copy. Don't re-peek the same gap.
3. **Act** — gesture tool with the grounded bbox.
4. **Verify** — `peek`. Listing identical to before = didn't land; retry once or re-target.
