# taobao

Doors, chosen by description at the boot. The marketplace, by
courier: `buy` buys one item off the 立即购买 sheet; `buy-together` buys
several in one order (`add` once per item, then `checkout`). The three
delivery storefronts inside the app, each with its own cart and order
page: `hema-buy` for 盒马 groceries (fresh food, ~30 min), `tmall-buy`
for 天猫超市 staples (packaged food, household goods, ~4 h), `shangou-buy`
for 淘宝闪购 takeout from one restaurant or shop (~30 min). Each `*-buy`
is `*-add` once per item then `*-checkout` (both `scope: local`);
`shangou-buy` adds every dish inside one store visit instead.

## Which door

The boot reads the request and picks by description: a store named
(盒马, 天猫超市 / 猫超, 外卖 / 闪购, a café or restaurant) decides; fresh
ingredients or 买菜 mean 盒马; something to eat or drink now means 闪购;
everyday staples wanted today mean 天猫超市; anything else — a product
with no store named — is the marketplace by courier (`buy`,
`buy-together`).

## Entries are searches, never the grid

The home feed's icon grid reorders, so no route taps it. 盒马 is reached
by searching 盒马 on the home feed (the main search jumps into its
storefront); 天猫超市 and every 闪购 store by the 闪购外卖 tab (a text
tab) and the 闪购 search, where the store's card comes first.

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
- A storefront's search field sits at the very top of its search page,
  so iOS draws the Paste bubble BELOW it; the home feed's field sits
  lower and its bubble comes up above (`search` vs `store-search` /
  `shangou-search` — the 闪购 page's bubble sits further right).
- The three storefronts' carts are separate from the marketplace cart,
  and their order pages print 合计, never 实付 — each route's README
  records its own.
- The buy sheet shows `￥…起` until a spec is chosen, and an option row
  tap sometimes needs a second tap.
