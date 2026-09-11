# taobao/buy-together

From one message naming several items to one paid order. `parse` lists
the items, `ack` tells the buyer what was understood, `add` runs once
per item (each round cold-starts Taobao, searches, picks, adds to the
cart; a round that cannot find its item is recorded as missed and the
rest go on), `checkout` ticks exactly those lines, confirms 实付款 off
the order page with the buyer, and pays once. A reply to that
confirmation that is neither yes nor no re-runs `parse` with the reply
and the cart lines: new items get a round, an item already in the cart
is not added again, a dropped item is left unticked. Two revisions,
then the reply hands over.
