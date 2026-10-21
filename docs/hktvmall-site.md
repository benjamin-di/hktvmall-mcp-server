# HKTVmall's endpoints: what is known

HKTVmall has no public API. This server uses the endpoints HKTVmall's website itself calls, and this page records
what has been observed about them (October 2026). `src/hktvmall_mcp/site.py` is the only module that sends
requests, and `site.PATHS` (pinned by `tests/test_safety.py`) lists every path it may reach. The fakes in
`tests/conftest.py` reproduce the quirks below, so the tests never contact HKTVmall.

When something here stops being true, calls fail with `schema_changed` (or the canary in `hktv_status(check=true)`
names the area, such as `orders_schema_changed`): compare with this page, fix `site.py`, add a test.

## Hosts

- `www.hktvmall.com`: the shop (pages, basket, order history, delivery slots). Every page answers 502 to connections
  from outside Hong Kong.
- `keyword-search-server.hktvmall.com`: search (KWS).
- `ucapi.comms.hktvmall.com`: public product reviews; no session is sent there.
- Images: `images.hktv-img.com`, `images.hktvmall.com`, `cdn-media.hktvmall.com`, `cdn-mms.hktvmall.com` (the
  product cards load photos from these only; they refuse HEAD, so check with GET).

## Search

- `POST https://keyword-search-server.hktvmall.com/api/search` with the `ApiKey` the home page embeds
  (`keywordSearchServerApiKey`), many queries per request, and `attributesToRetrieve` (20 queries: 1.35 MB → 98 KB,
  0.14 s). It needs a browser User-Agent.
- English search is weaker than Chinese ("firm tofu" finds tofu cat litter; 硬豆腐 finds tofu).
- A price limit is `range: {"sellingPrice": "-19.9"}` (inclusive, decimals), as HKTVmall's own client sends it.
  Unknown request keys are ignored silently; there is no price-sorted index.
- Search can fail silently: for minutes at a time it has answered `KWS-200` with no hits for every query. An
  all-empty answer is believed only if a probe search for a common item has hits; otherwise search and products carry
  `notes: [{code: search_empty}]` and a basket change stops before writing anything (`upstream_error`).
- A product missing from search can come back days later; `not_found` means "not sold right now".
- Stock codes: `inStock`, `lowStock` (still buyable), `stockNotifiable` and `outOfStock` (not buyable).
- A shop on holiday has `storeClosedStatus: 1` and a `closed-store` window in `deliveryLabelScheduleCode`; search
  still shows its items in stock, but an add answers success and is dropped (`store_closed`).
- Product pages: each hit's `urlEn` is the canonical page path (`main/<store>/s/<store code>/<categories>/<name>/p/<id>`,
  percent-encoded); `/hktv/en/p/<id>` only redirects (301) to it. `imageLink` is a 300 px photo (65, 96, 515 and
  1200 px variants exist); `gallery` holds every photo of the listing at 1200 px (also at 515 px), the primary first.
- Ids: every product id matches `^[A-Z][0-9]{7}_S_`; about a quarter contain `-`. Reviews are keyed by
  `baseProduct`, usually the id without `_S_`, but variants share another one.

## Session

- Signing in happens in a browser (password, a checkbox or image challenge, and a one-time code by email or SMS for
  a new browser). The resulting cookies work over plain HTTP.
- `access_token` (about 30 days) is minted by a plain GET of `/hktv/en/cart` and cleared by every legacy (Hybris)
  cart call (`/hktv/en/cart/add`, `updateEntry`), so the server re-mints it after adds.
- `JSESSIONID` stays the same across normal reads; only `access_token` rotates.
- The home page says whether the session is signed in (`'loggedin' : true|false`) and whose it is
  (`realContactEmail`). Guests have baskets too, so every new session is checked on the home page before its first
  basket call.

## Basket

- Read: `GET /en/hktvfrontend/v1/cart/express/checkout`: delivery-date groups (HKTVmall's own delivery: HK$40 below
  HK$88; merchant delivery with its own fees) and `stockLevel` per line.
- Add exists only as the legacy `POST /hktv/en/cart/add`; update is `update_item_in_cart`, removal
  `remove_item_from_cart`.
- **Write replies are not evidence**: `success` for an item that was silently dropped (out of stock, express-only),
  `SUCCESS` for 500 stored as 51 (the stock level), `quantityAdded: 50` with no change. Hence the read-back.
- `update_item_in_cart` with quantity 0 leaves a "zombie" line: every basket read then returns a 403 "maintenance"
  page until `remove_item_from_cart` removes it. The server never sends quantity 0 to update and repairs once if a
  read fails after a removal.
- Fees and totals: `hktvDeliveryFee` is the order summary's "HKTV Delivery Fee" line and follows the threshold;
  `deliveryCost` stays HK$40 (it only fills the "HK$40 below HK$88" tip). The summary's "Total payment" is
  `cartData.totalPriceWithTax`; `otherFeeRemarks` lists surcharges such as fuel.
- Checkout warnings (`errorMessages`) are plain strings; the checkout page merges the basket's list with
  `get_checkout_error_message`'s.
- A basket 5xx is temporary (`upstream_error`), not a format change.

## Orders

- `GET /hktv/en/ajax/getOrderList` is JSON, five orders a page, processing and completed orders paged separately.
- An order's items exist only on `/hktv/en/my-account/order/<number>` (about 2 MB of HTML) as
  `var orderWithBatches = {...}`: `entryPrice` is the line total paid (lines sum to the order total) and `FREE`
  marks a gift. The same object carries the address and payment, which the server never reads.
- `ajax/addToCartWithOrderNumber` re-adds a whole order on top of the basket; it is not used (re-orders go line by
  line, with caps and read-back).

## Delivery slots

- `get_checkout_addresses` (the checkout uses `data[0]`) gives `zoneId`;
  `get_available_delivery_time_slots?deliveryZoneId=` gives `deliveryTimeSlots[]` (`formattedValue`,
  `isNotAvailable`), the default `deliveryTimeSlot`, `chosenByUser`, `deliveryFee` and `deliveryThreshold`.
- For an empty basket it answers `status: FAIL` with only the fee fields (fees as 0.0); any other `FAIL` is a format
  change.

## Reviews

- `ucapi.comms.hktvmall.com/hktvmall/products/<baseProduct>/reviews/` and `/reviews/stat`, public; pages start at 0,
  newest first (the site's client calls `requestReviewsAction(productCode, 0, ...)`). 204 means no reviews; comments
  are HTML-escaped and mostly Chinese. A 429 there fails only that call.

## Pushback

- A 429 from the shop or search, or a 403 on the home page or search, means HKTVmall is pushing back: everything
  pauses for 5 minutes, or 30 when it happens again within two hours. A single 403 after a 30 s hang has been seen
  on an otherwise healthy site.

## Clients and the product cards

- `hktv_show_products` is linked to the MCP Apps view `ui://hktvmall/products.html` (`text/html;profile=mcp-app`).
  claude.ai fetched the view only once its tool metadata carried `ui` alone and the CSP listed exact origins (no
  wildcard hosts, no empty `connectDomains`); it re-reads resource lists only every few hours, so a `ttlMs` of 10
  minutes on the lists lets a new release reach it sooner.
- claude.ai on the web and on phones renders the cards; at the time of writing the Windows desktop app did not
  declare the extension and showed the result as text.
- HKTVmall's iPhone app claims every hktvmall.com link (`apple-app-site-association` `"/": "*"`), so links open in the
  app when it is installed.
