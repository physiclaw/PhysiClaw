# taobao

Four playbooks. `buy` buys one item off the 立即购买 sheet: search,
pick, approve the sheet total, pay, report. `buy-together` buys several
items in one order: `add` (one item into the cart, also a playbook of
its own) once per item, then `checkout` (the cart to a paid order, also
its own: "把购物车结算了"). The boot offers them by description.

## Device

Recorded on an iPhone with the system in English and Taobao in
Chinese. The pack's pages (`home`, `results`, `buysheet`, `paid`) are
declared by anchors only; run `physiclaw playbooks pages calibrate
taobao` on your phone so one OCR miss does not read a page unknown.

## Rehearsal

`launch`, `search` and `foreground` are pack macros every route shares;
each ships disabled and needs `physiclaw macros run taobao/<name>` on
your device before a playbook that uses it is live.

## Traps

- `force_quit` kills the foreground app, so `launch` raises Taobao
  before quitting it; a resumed app lands mid-state and fails verify.
- Two-character anchors (综合, 销量) match feed text unless pinned
  `within: top`.
- The detail footer's third icon is 收藏, not the cart; `buy` never
  touches the cart, it buys off the 领券购买 / 立即购买 sheet. The cart
  flow reaches the cart by the home footer's 购物车 tab (checkout's `open-cart`).
- The 加入购物车 toast is transient, so `add` verifies the detail page
  and the cart itself is the check: a line `tick` cannot find escalates.
- Promo popups (天降红包, coupons) cover a page right after it opens;
  `landmarks.close` is their ✕, never their buttons.
- The buy sheet shows `￥…起` until a spec is chosen, and an option row
  tap sometimes needs a second tap.
