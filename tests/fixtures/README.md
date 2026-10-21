# Test fixtures

Every fixture here is synthetic. Each one is shaped like what HKTVmall sends (the same keys, nesting, value types and
formats: `"$ 12.30"` strings beside plain numbers, `+08:00` timestamps, product ids, page paths and image hosts) but
every value is invented: products, shops, prices, stock, dates, order numbers, reviews and sessions.

- `checkout.json`, `search_hits.json`, `order_list.json`, `order_H*.html`, `delivery_slots.json`,
  `review_stat.json`, `reviews.json`: HKTVmall's payloads cut down to the keys the code reads
  (`tests/test_contract.py` enforces the checkout allowlist). The order pages and reviews keep decoy `address`,
  `payment` and `user` fields the parsers must drop; `checkout_addresses.json` has a test zone and decoy fields.
- `pack_sizes.json`: invented `[packingSpecEn, sellingPrice]` pairs that reach every rule in
  `src/hktvmall_mcp/units.py`; `tests/test_contract.py` lists what each must parse to.
- `session-snapshot.json`: a session file as a publisher writes it (one line, its keys in that order), for
  `shopper@example.com` with fake cookie values (`test-jsessionid`, ...).

Never commit a payload recorded from a real account, even trimmed. To add a fixture, copy the response's shape from a
guest session (search, a guest basket and reviews need no sign-in), keep only the keys the code reads, and invent every
value in the same formats.
