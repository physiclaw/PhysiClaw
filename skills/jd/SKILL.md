---
name: jd
description: Use when the task is grocery / fresh-food shopping via JD / 京东 / 七鲜 / 7Fresh — user asks to buy 零食, 水果, 蔬菜, 日用品, or to place an order on 京东. NOT for clothing, electronics, or non-grocery shopping, NOT for other grocery apps (Meituan / 盒马 / Dingdong).
---

# JD (京东) — Grocery shopping

Use **京东七鲜** (JD 7Fresh) for groceries. Other JD categories require explicit user ask.

`peek` is the default; `screenshot` only when the target is icon-only with no text label. **`screenshot` on a product detail page triggers a share overlay** (see Gotcha).

## Flow

1. `Skill(name="open-app")` → `京东` (or `JD`). On **首页**, tap into 京东七鲜.
2. Tap the search box. Stale text? Tap backspace until empty (or use `Skill(name="search-in-app")` for the full clear-paste-submit flow). Type/paste the item, open its product page.
3. Tap **加入购物车** (Add to cart).
   - **Spec sheet** (size, brand, weight) often slides up — pick the spec, tap **确定**. Sheet dismisses, item added, you land back on the product page.
   - Items without variants skip the sheet — one tap adds directly.
   - **NEVER tap 加入购物车 twice on the same product page.** The page looks identical before and after — re-tapping re-opens the spec sheet, you re-confirm, and the item is added TWICE. Verify via the cart-icon badge or in step 4.
4. Tap the cart icon, review line items, tap **去结算**. **Always review here** — duplicate quantities mean step 3 was tapped twice; remove dupes before checkout.
5. Send the user: item, qty, price, address, fees, ETA. Wait for explicit OK.
6. Tap **提交订单 / 立即支付**.

## Gotcha — screenshot triggers share overlay

JD intercepts the iOS screenshot gesture and overlays a 分享截屏 menu (朋友圈 / QQ / 微信好友 / 保存图片 / 搜问题). The screenshot captured the real page first; recover without re-shooting:

1. `screenshot()` — pixel-perfect bboxes captured.
2. `peek()` — share-sheet text + dim area = popup is covering the page.
3. Dismiss (see below).
4. `peek()` — confirm overlay is gone.
5. **Act on labelled targets from the screenshot's text rows.** Icon rows drop after the screenshot is stubbed, but text rows (`加入购物车`, product title, price, `去结算`, `+` add buttons that OCR reads as glyphs) survive — tap those. Pure icon-only target → re-`screenshot` (rare).

## Dismiss popup / share sheet / bottom sheet

Tap the **dimmed area** above the popup. Safe target: `[0.05, 0.40, 0.15, 0.60]` (left edge, vertical middle). Mirror `[0.85, 0.40, 0.95, 0.60]` works too.
