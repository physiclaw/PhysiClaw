# taobao/shangou-buy

Takeout from one store on 淘宝闪购. `parse` names the store and the
dishes, `ack` tells the buyer, the walk opens the 闪购外卖 tab and searches
the store by name, one `order` episode taps its card and adds every dish
through its spec sheet (选规格 → 选好了), `settle` presses 去结算, the
buyer confirms the total off the order page, `pay` presses 立即支付.

## Device

Recorded facts: 闪购 search results are STORE cards, each with a
few dishes below it; a store's menu shows 外送 / 自取 up top, 选规格 or ＋
per dish, and a cart bar (￥…, 起送, 去结算) at the bottom. The order
page prints 外卖到家 / 到店自取, the address, 立即送出 + a slot, the
store as 闪购<name>, 商品原价 / 打包费 / 配送费 / 红包, 合计, and a
立即支付￥N button. One order is one store.
