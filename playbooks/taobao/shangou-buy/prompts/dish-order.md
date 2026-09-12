You are on 淘宝闪购's search results for "{parse.store}": STORE cards
(name, rating, 起送, delivery time), each followed by a few of its
dishes. First open the store, then add exactly these dishes to its
cart, then stop:
{parse.dishes}
OPEN — tap the card whose NAME matches "{parse.store}" (the first
matching card; a branch the buyer named, when they named one). Tap
the name row, never a dish under it. The store's own page shows
外送 / 自取 at the top. No card names that store: scroll once; still
none, return escalate and say what the results show.
ADD — for each listed dish, in order:
1. Find it in the menu (the category tabs on the left switch the
   list; scroll the list at most twice per dish). Tap its ＋, or its
   选规格 button when it has one.
2. On the spec sheet choose the rows the dish names (规格 / 温度 /
   糖度 / 加料); leave a row at its default when the dish says
   nothing about it; set 数量 to the ×N the list says; tap 选好了
   once. The sheet closes — the dish is in the cart.
The cart bar at the bottom shows the running total: it must grow
after each add. When every listed dish is added, return done with
`added`: the dishes now in the cart, one per line, as the store
names them, with the spec chosen. A dish that is 售罄 or not on this
menu: skip it, and say so in `added` as "(not available) <dish>".
