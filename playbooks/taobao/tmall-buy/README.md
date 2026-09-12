# taobao/tmall-buy

Supermarket staples from 天猫超市 inside Taobao, several items in one
order. The walk launches Taobao, opens the 闪购外卖 tab, searches 天猫超市
there and opens its store card (`open-tmall`), then `tmall-add` runs once
per item from the store's page and `tmall-checkout` finishes, with a
revision path back to `parse` as in `buy-together`.

## Device

Recorded facts: 天猫超市 is a store inside 淘宝闪购 — the 闪购 search
for 天猫超市 puts 天猫超市闪购（北京店） first. The store page shows
天猫超市闪购 in its header, 全部商品, and its own search; its cart is a
sheet the footer total (到手约￥…) opens, with 全选（已选N件）, 清空 and
去结算; the order page prints 闪购天猫超市闪购（…店）, 合计￥ and 提交订单.
