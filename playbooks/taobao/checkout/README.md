# taobao/checkout

The cart to one paid order: `launch` cold-starts Taobao and `open-cart`
taps the 购物车 tab, `tick` makes the ticked lines match the buyer's list (unticking
everything else, quantities included), `checkout` taps 结算, the
`confirm-pay` ask quotes 实付款 off the order page (shipping and
discounts applied) and waits for 好的 / 不用, `pay` submits the order
and pays. Runs alone for "把购物车结算了" (`lines` 全部, the default, keeps whatever
is ticked) and as the last move of `buy-together`.
