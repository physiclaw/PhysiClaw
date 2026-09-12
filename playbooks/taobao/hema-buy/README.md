# taobao/hema-buy

Groceries from 盒马 inside Taobao, several items in one order. `parse`
lists the items, `ack` tells the buyer, the walk launches Taobao and
searches 盒马 on the home feed (the main search jumps into the
storefront), `hema-add` runs once per item from the storefront's home
(its own search, pick, 加入购物车, back to home), then `hema-checkout`
opens the storefront's cart, ticks exactly those lines, confirms 合计
off the order page and pays once. A reply that is neither yes nor no
re-runs `parse` with the cart lines, as `buy-together` does.

## Device

Recorded facts: the storefront shows 盒马 + address in the header,
权益中心, and its own 首页/分类/购物车/我的 bar. Its cart page lists lines
with a checkbox each, 全选（已选N件） and 清空 above, 去结算 in a footer
that sits higher than 天猫超市's; the order page is 确认订单 with
合计：￥ and 提交订单.
