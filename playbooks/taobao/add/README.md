# taobao/add

One item into the cart, nothing paid: `parse` derives the keyword,
`launch` cold-starts Taobao, `search` types the keyword, `pick` opens
the listing and adds it through the 加入购物车 sheet with the quantity
the buyer named, and the walk ends on the detail page. Returns `line`,
the cart line as the buyer will read it. Runs alone for "把 X 加入购物车"
and once per item inside `buy-together`, where its `start` is the reset
between items.
