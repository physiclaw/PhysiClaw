---
name: jd
description: Use when the task is grocery / fresh-food shopping via JD / 京东 / 七鲜 / 7Fresh — owner asks to buy 零食, 水果, 蔬菜, 日用品, or to place an order on 京东. NOT for clothing, electronics, or non-grocery shopping, NOT for other grocery apps (Meituan / 盒马 / Dingdong).
---

# JD (京东) — Grocery shopping

Use **京东七鲜** (JD 7Fresh) for groceries. Other JD categories need explicit owner ask.

## Tool choice

Prefer `peek()` — cheap, no app-side reactions. Reach for `screenshot()` only when the target is icon-only (no text label). **`screenshot()` on a commodity detail page triggers the share overlay** — see Gotcha.

## Flow

1. `/open-app 京东` (or `JD`). Make sure you're on the **首页** tab (top nav), then tap into 京东七鲜.
2. Tap the search box. If it has stale text, tap **backspace** (bbox in PHYSICLAW.md "iPhone keyboard bboxes") until empty — see also `Skill(name="search-in-app")` for the full clear-paste-submit flow. Type/paste the item, open its shop page.
3. Tap 加入购物车 (Add to cart).
   - **A spec-selection sheet often slides up** (size, brand, weight). Pick the right spec, then tap **确定** to confirm. The sheet dismisses, the item is added, and you land back on the product page.
   - Items without variants skip the sheet — one tap on 加入购物车 adds directly.
   - **NEVER tap 加入购物车 again on the product page.** The product page looks the same before and after add — you cannot tell from the layout alone whether the add succeeded. Re-tapping just re-opens the spec sheet, you re-confirm, and you've now added the item TWICE. Trust that 确定 worked; verify by checking the cart-icon badge count (top-right corner of the page) or by going to the cart in step 4.
4. Tap the cart icon, review line items, tap 去结算. **Always review the cart here** — if quantities look wrong (item appearing twice when you only meant once), that's a sign step 3 was tapped twice; remove the dup before checkout.
5. Send the owner: item, qty, price, address, fees, ETA. Wait for explicit OK.
6. Tap 提交订单 / 立即支付.

## Gotcha — screenshot triggers share popup

JD intercepts the iOS screenshot gesture and overlays a 分享截屏 menu (朋友圈 / QQ / 微信好友 / 保存图片 / 搜问题). The screenshot still captured the real page — pin the bboxes you need before the overlay forces another view:

1. `screenshot()` — pixel-perfect bboxes from the pre-overlay page.
2. **Next turn: pin targets in `note.key_ui_elements`, then `peek`.** The `peek` will stub the screenshot's listing out of history, but the pinned bboxes survive. Example:
   ```
   note(
     summary="pinning product-page targets before share-sheet check",
     screen="JD product page (screenshot pre-overlay)",
     key_ui_elements={
       "add_to_cart": {"kind": "icon", "label": "加入购物车", "bbox": [...]},
       "cart_icon":   {"kind": "icon", "label": "shopping cart top-right", "bbox": [...]},
     },
   )
   peek()
   ```
3. If peek shows share-sheet text + dim area, dismiss the popup (see below).
4. `peek` again to confirm the overlay is gone.
5. **Tap using the pinned bboxes** — `tap(bbox=<pinned add_to_cart bbox>)`. Saves ~12s vs retaking a screenshot and avoids triggering the overlay a second time.

## Dismiss popup / share sheet / bottom sheet

Tap the **dimmed area** above the popup. Safe target: `[0.05, 0.40, 0.15, 0.60]` (center ≈ x=0.10, y=0.50 — left edge, vertical middle). The mirror bbox on the right edge `[0.85, 0.40, 0.95, 0.60]` works too.
