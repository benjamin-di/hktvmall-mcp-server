"""Contract tests against payloads shaped like HKTVmall's own (tests/fixtures/README.md).

The fixtures keep HKTVmall's keys, nesting, types and formats with invented values. The hand-written fakes elsewhere
encode what we believe; these encode the shapes HKTVmall actually sends, so a wrong belief (the flat HK$40
deliveryCost read as a charge) fails here.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from urllib.parse import urlencode

import pytest
from test_site import account

from hktvmall_mcp import cart, server
from hktvmall_mcp.errors import HktvError
from hktvmall_mcp.site import PATHS

FIXTURES = Path(__file__).parent / "fixtures"
CHECKOUT_KEYS = {
    "cartData.totalPriceWithTax.formattedValue",
    "cartData.totalPriceWithTax.value",
    "cartDataGroupByDeliveryDate.deliveryDateOrderEntries[].batchNumber",
    "cartDataGroupByDeliveryDate.deliveryDateOrderEntries[].deliveryMode",
    "cartDataGroupByDeliveryDate.deliveryDateOrderEntries[].freeDelivery",
    "cartDataGroupByDeliveryDate.deliveryDateOrderEntries[].groupTitle",
    "cartDataGroupByDeliveryDate.deliveryDateOrderEntries[].orderEntries[].basePrice.formattedValue",
    "cartDataGroupByDeliveryDate.deliveryDateOrderEntries[].orderEntries[].basePrice.value",
    "cartDataGroupByDeliveryDate.deliveryDateOrderEntries[].orderEntries[].discountedUnitPrice.formattedValue",
    "cartDataGroupByDeliveryDate.deliveryDateOrderEntries[].orderEntries[].discountedUnitPrice.value",
    "cartDataGroupByDeliveryDate.deliveryDateOrderEntries[].orderEntries[].product.code",
    "cartDataGroupByDeliveryDate.deliveryDateOrderEntries[].orderEntries[].product.name",
    "cartDataGroupByDeliveryDate.deliveryDateOrderEntries[].orderEntries[].product.stock.stockLevel",
    "cartDataGroupByDeliveryDate.deliveryDateOrderEntries[].orderEntries[].product.url",
    "cartDataGroupByDeliveryDate.deliveryDateOrderEntries[].orderEntries[].quantity",
    "cartDataGroupByDeliveryDate.deliveryDateOrderEntries[].orderEntries[].totalPrice.formattedValue",
    "cartDataGroupByDeliveryDate.deliveryDateOrderEntries[].orderEntries[].totalPrice.value",
    "cartSubTotal.formattedValue",
    "cartSubTotal.value",
    "deliveryCost",
    "deliveryThreshold",
    "hktvDeliveryFee.value.formattedValue",
    "hktvDeliveryFee.value.value",
    "otherFeeRemarks[].code",
    "otherFeeRemarks[].fee.formattedValue",
    "otherFeeRemarks[].fee.value",
}


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def leaf_keys(o, p=""):
    if isinstance(o, dict):
        for k, v in o.items():
            yield from leaf_keys(v, f"{p}.{k}" if p else k)
    elif isinstance(o, list):
        for v in o:
            yield from leaf_keys(v, f"{p}[]")
    else:
        yield p


def test_checkout_fixture_holds_only_the_keys_the_view_reads():
    # HKTVmall's checkout payload also carries the account's address and contact: a fixture keeps only these keys.
    assert set(leaf_keys(load("checkout.json"))) <= CHECKOUT_KEYS


def test_a_free_delivery_basket_matches_hktvmalls_order_summary():
    totals = cart.view(load("checkout.json"))["totals"]
    assert totals["delivery_fee"]["cents"] == 0  # deliveryCost still says $40 in this payload, as HKTVmall's does
    assert totals["subtotal"]["display"] == "HK$367.50"
    assert totals["other_fees"]["display"] == "HK$2.10"  # plastic bag levy and fuel surcharge
    assert totals["total"]["display"] == "HK$369.60"


def test_search_hits_label_delivery_and_closed_shops():
    milk, oil = sorted(load("search_hits.json"), key=lambda h: h["code"])
    m = server.product(milk, details=True)
    assert m["delivery"] == "8-Hour Delivery" and m["origin"] == "New Zealand" and "Shelf life" in m["summary"]
    assert "summary" not in server.product(milk)  # search stays lean
    assert server.product(oil)["availability"] == "store_closed"
    assert server.product(oil)["reopens_at"] == "2030-03-04T13:00+08:00"  # the end of its closed-store window


def test_products_link_to_hktvmalls_own_page_and_photo():
    # Links were once built from a template (/hktv/en/p/{id}): a redirect on the web, unknown to HKTVmall's app.
    for hit in load("search_hits.json"):
        lean, full = server.product(hit), server.product(hit, details=True)
        assert lean["url"] == "https://www.hktvmall.com/hktv/en/" + hit["urlEn"] and lean["url"].endswith(hit["code"])
        assert "image" not in lean  # search stays lean; get_products and the cards have the photo
        assert full["image"] == hit["imageLink"]
    for line in cart.view(load("checkout.json"))["line_items"]:
        assert line["url"].startswith("https://www.hktvmall.com/hktv/en/main/") and line["url"].endswith(line["id"])


def order_account():
    query = urlencode({"lang": "en", "completedCurrentPage": 0, "processingCurrentPage": 0, "pageSize": 5})
    answers = {PATHS["order_list"] + "?" + query: [(200, (FIXTURES / "order_list.json").read_text(encoding="utf-8"))]}
    for page in FIXTURES.glob("order_*.html"):
        answers[PATHS["order_page"] + page.stem.removeprefix("order_")] = [(200, page.read_text(encoding="utf-8"))]
    return account(answers)


def test_order_pages_give_items_and_never_the_address():
    acct, _ = order_account()
    listing = acct.order_list(0)
    assert listing["total"] == 37 and [o["code"] for o in listing["completed"]] == ["H900000000001", "H900000000002"]
    entries = acct.order_entries("H900000000002")
    rows = [server.orders.item(e) for e in entries]
    assert [(r["quantity"], r["line_total"]["display"], r.get("free_gift", False)) for r in rows] == [
        (2, "HK$71.50", False),
        (1, "HK$0.00", True),
        (2, "HK$179.80", False),
    ]
    assert "DECOY" not in json.dumps(rows)


def test_order_numbers_are_validated_before_any_request():
    acct, site = order_account()
    with pytest.raises(HktvError):
        acct.order_entries("../cart/add")
    assert site.transport.calls == []


def test_search_sends_the_price_limit_to_hktvmall():
    acct, site = account({})
    sent = []
    site.search_key = "k"
    site._post_search = lambda reqs: (sent.append(reqs), (200, json.dumps({"results": [{"hits": []}]})))[1]
    site.search([{"q": "milk", "limit": 3, "max_price_cents": 1990}, {"q": "eggs", "limit": 3}])
    assert sent[0][0]["range"] == {"sellingPrice": "-19.9"} and "range" not in sent[0][1]


def test_a_dropped_add_is_logged_with_hktvmalls_reply(caplog):
    from conftest import FakeSite, product

    site = FakeSite(products=[product("OIL")], drop_on_add={"OIL"})
    with caplog.at_level(logging.INFO, logger="hktvmall_mcp.cart"):
        r = cart.update(site, [{"id": "OIL", "quantity": 1}], False)
    assert r["changes"][0]["reason"] == "not_accepted"
    assert "change OIL: rejected not_accepted" in caplog.text and "'status': 'success'" in caplog.text


def test_delivery_zone_reads_only_the_first_addresses_zone():
    raw = (FIXTURES / "checkout_addresses.json").read_text(encoding="utf-8")
    acct, site = account({PATHS["addresses"]: [(200, raw)]})
    assert acct.delivery_zone() == "ZONE_TEST_1"
    assert site.transport.calls == [PATHS["addresses"]]


def test_a_missing_zone_is_an_error_not_a_guess():
    acct, _ = account({PATHS["addresses"]: [(200, json.dumps({"status": "SUCCESS", "data": []}))]})
    with pytest.raises(HktvError) as e:
        acct.delivery_zone()
    assert e.value.code == "schema_changed"


def test_delivery_slots_view():
    from hktvmall_mcp import views

    v = views.slots(load("delivery_slots.json")["data"])
    assert v["covers"].startswith("HKTVmall delivery") and len(v["slots"]) == 10
    assert v["slots"][0] == {"label": "5 Mar (Tue) 10:00AM - 2:00PM", "available": True}
    assert v["slots"][2] == {"label": "7 Mar (Thu) 10:00AM - 2:00PM", "available": False}  # isNotAvailable
    assert v["fee"]["display"] == "HK$0.00" and v["free_from"]["display"] == "HK$88.00"
    assert v["selected"] == "6 Mar (Wed) 10:00AM - 2:00PM"  # chosenByUser is true in the fixture
    assert "selected" not in views.slots({**load("delivery_slots.json")["data"], "chosenByUser": False})


def test_reviews_view_drops_reviewers_and_unescapes():
    from hktvmall_mcp import views

    v = views.reviews({"stat": load("review_stat.json"), "page": load("reviews.json")}, 3)
    assert v["average"] == 4.2 and v["count"] == 240 and v["total_reviews"] == 252
    assert list(v["by_rating"]) == ["5", "4", "3", "2", "1"] and v["by_rating"]["1"] == 18
    assert len(v["reviews"]) == 3 and all(
        set(r) == {"rating", "date", "comment", "tags", "merchant_replied"} for r in v["reviews"]
    )
    assert "DECOY" not in json.dumps(v) and "&#41;" not in json.dumps(v)
    assert "Poor packaging" in v["reviews"][0]["tags"]
    assert v["reviews"][1]["comment"] == "Came a day late & one carton had split open (the rest were fine)"


def test_reviews_reject_a_non_product_id_before_any_request():
    acct, site = account({})
    with pytest.raises(HktvError) as e:
        site.reviews("../../x", 3, None)
    assert e.value.code == "invalid_argument" and site.transport.calls == []


def test_reviews_ask_the_public_host_by_base_product_and_treat_204_as_none():
    acct, site = account({})
    sent = []
    site._public = object()

    def send(opener, method, url, body=None, headers=None):
        sent.append((opener, url))
        return (204, "") if "/reviews/?" in url else (200, (FIXTURES / "review_stat.json").read_text(encoding="utf-8"))

    site.transport.send = send
    out = site.reviews("H999000120000001B", 3, 1)
    assert all(o is site._public for o, _ in sent)
    assert sent[0][1].startswith("https://ucapi.comms.hktvmall.com/hktvmall/products/H999000120000001B/reviews/stat?")
    assert "rating=1" in sent[1][1] and out["page"] == {} and out["stat"]["count"] == 240
    # HKTVmall's own client asks for page 0 first (requestReviewsAction(productCode, 0, ...)): newest first.
    assert "current_page=0" in sent[1][1] and "lang=en" in sent[1][1] and "page_size=3" in sent[1][1]


def test_reviews_are_filed_under_searchs_base_product():
    # The old id rule refused ids HKTVmall uses (about a quarter carry "-", like B9990006_S_Brand-0099); variants
    # file their reviews under a shared base product that is not the id without "_S_".
    from hktvmall_mcp.site import Site

    milk = next(h for h in load("search_hits.json") if h["code"] == "H9990001_S_20000001B")
    assert Site.review_key(milk["code"], milk) == milk["baseProduct"] == "H999000120000001B"
    variant = {"baseProduct": "H999000710_grains_family"}
    assert Site.review_key("H9990007_S_10_Grains_Rice_1000g", variant) == "H999000710_grains_family"
    assert Site.review_key("H9990008_S_A1234-B-CD50", None) == "H9990008A1234-B-CD50"  # not listed: the id rule
    assert Site.review_key("H9990008_S_A1234-B-CD50", {"baseProduct": "../x"}) == "H9990008A1234-B-CD50"
    for bad in ("../../x", "H9990008A1234", "H9990008_S_a/b", "H9990008_S_" + "x" * 71):
        with pytest.raises(HktvError):
            Site.review_key(bad, None)


def test_sample_reviews_are_newest_first_and_entities_are_unescaped():
    from hktvmall_mcp import views

    rows = load("reviews.json")["data"]
    dates = [r["date"] for r in rows]
    assert dates == sorted(dates, reverse=True) and load("reviews.json")["pagination"]["page"] == 0
    escaped = {"stat": {}, "page": {"data": [{"rating": "1", "comment": "地面&#41; 交收 &amp; more"}]}}
    assert views.reviews(escaped, 1)["reviews"][0]["comment"] == "地面) 交收 & more"


def test_requests_copy_hktvmalls_own_client():
    # Each request shape below was read from HKTVmall's own web client (docs/hktvmall-site.md); a guess is
    # how reviews came to skip the newest page, so every shape is pinned here.
    from urllib.parse import parse_qs, urlsplit

    zone_raw = (FIXTURES / "checkout_addresses.json").read_text(encoding="utf-8")
    slots_raw = (FIXTURES / "delivery_slots.json").read_text(encoding="utf-8")
    slots_path = PATHS["delivery_slots"] + "?deliveryZoneId=ZONE_TEST_1"
    acct, site = account({PATHS["addresses"]: [(200, zone_raw)], slots_path: [(200, slots_raw)]})
    acct.delivery_slots(acct.delivery_zone())
    assert site.transport.calls == [PATHS["addresses"], slots_path]
    query = urlencode({"lang": "en", "completedCurrentPage": 2, "processingCurrentPage": 0, "pageSize": 5})
    acct, site = account(
        {PATHS["order_list"] + "?" + query: [(200, (FIXTURES / "order_list.json").read_text(encoding="utf-8"))]}
    )
    acct.order_list(2)
    q = parse_qs(urlsplit(site.transport.calls[0]).query)
    assert q == {"lang": ["en"], "completedCurrentPage": ["2"], "processingCurrentPage": ["0"], "pageSize": ["5"]}


# What units.parse must read from each pack size in tests/fixtures/pack_sizes.json, None where it must refuse. The
# sample is invented, written the ways HKTVmall's catalogue writes sizes, and reaches every rule in units.py:
# multipliers, counted packs, brackets, a "pack" suffix, ranges, mixes, alternatives, two measures and free text.
PACK_SIZES = {
    "1000ml x 3": ("ml", 3000), "1L x 4": ("ml", 4000), "12x1L": ("ml", 12000), "250ml x 8": ("ml", 2000),
    "110ml x 4 x 3": ("ml", 1320), "1.5L": ("ml", 1500), "946ml": ("ml", 946), "500 milliliters": ("ml", 500),
    "2 Ltr": ("ml", 2000), "1 Litre x 6": ("ml", 6000), "5 Liter": ("ml", 5000), "454g": ("g", 454),
    "1kg x 2": ("g", 2000), "5kg x 2": ("g", 10000), "200gm": ("g", 200), "90g x 5 pcs": ("g", 450),
    "6 x 330ml": ("ml", 1980), "4 packs x 100g": ("g", 400), "500g*2": ("g", 1000), "1kg×2": ("g", 2000),
    "6-slice (230g)": ("g", 230), "200ml x 6 Family Pack": ("ml", 1200), "1kg Pack": ("g", 1000),
    "330ml x 24 cans": ("ml", 7920), "3 bottles x 500ml": ("ml", 1500), "500g.": ("g", 500),
    "10 bags x 2g": ("g", 20), "100g x 3 pk": ("g", 300), "1 x 2kg": ("g", 2000), "1L": ("ml", 1000),
    "24pcs": ("piece", 24), "36 rolls": ("roll", 36), "10pcs x2": ("piece", 20), "1 roll": ("roll", 1),
    "12 eggs": ("piece", 12), "3 pieces": ("piece", 3), "1 piece": ("piece", 1), "5pc": ("piece", 5),
    "10 rolls x 3": ("roll", 30),
    "": None, None: None, "530ml+530g": None, "1L / 1000ml": None, "180g-240g x2": None, "100~120g": None,
    "27s": None, "2lb": None, "16oz": None, "Licensed Product": None, "Drained weight 100g": None,
    "1L x 500ml": None, "0ml": None, "6 x 2": None, "3 bags": None, "10pcs x 2 x 3": None, "12 cans": None,
    "1 Box": None, "Assorted": None,
}  # fmt: skip


def test_unit_prices_on_sample_pack_sizes():
    from hktvmall_mcp import units

    rows = load("pack_sizes.json")
    assert {spec for spec, _ in rows} == set(PACK_SIZES)  # every size in the sample has its expected reading
    for spec, _ in rows:
        assert units.parse(spec) == PACK_SIZES[spec], spec
    assert units.unit_price(5490, "1000ml x 3") == {"cents": 183, "per": "100ml"}
    assert units.unit_price(8990, "5kg x 2") == {"cents": 90, "per": "100g"}
    assert units.unit_price(4560, "24pcs") == {"cents": 190, "per": "piece"}
    assert units.unit_price(9900, "36 rolls") == {"cents": 275, "per": "roll"}
    assert units.unit_price(3800, "Assorted") is None
    assert units.unit_price(None, "1L") is None and units.unit_price(100, None) is None


def test_basket_warnings_from_both_sources_become_notes(service, site):
    site.checkout_warnings = ["<b>Item A</b> is out of stock", "Item A is out of stock", ""]
    view = service.basket()
    assert view.notes == [{"code": "hktvmall_warning", "item_id": None, "text": "Item A is out of stock"}]


def test_order_tracking_from_the_order_list():
    from hktvmall_mcp import orders

    first = load("order_list.json")["closedPageData"]["results"][0]
    assert orders.view(first, [])["deliveries"] == [
        {"date": "2030-03-02T15:41:08+08:00", "status": "delivered", "by": "HKTVmall"}
    ]
    assert orders.delivery({"isCancelled": True, "isDelivered": True})["status"] == "cancelled"
    assert orders.delivery({"deliveryDateText": "5 Mar"}) == {"date": "5 Mar", "status": "scheduled"}


def test_a_change_never_sent_is_not_blamed_on_hktvmall():
    from conftest import FakeAccount, FakeSite, product

    site = FakeSite(lines={"A": 2, "B": 5, "C": 1}, products=[product("A"), product("B"), product("C")])
    real = FakeAccount.set_quantity

    def failing(self, sku, qty):
        if sku == "A":
            raise HktvError("upstream_error")
        return real(self, sku, qty)

    FakeAccount.set_quantity = failing
    try:
        r = cart.update(
            site, [{"id": "A", "quantity": 4}, {"id": "B", "quantity": 3}, {"id": "C", "quantity": 0}], False
        )
    finally:
        FakeAccount.set_quantity = real
    rows = {x["id"]: x for x in r["changes"]}
    assert rows["A"]["reason"] == "not_accepted"  # sent, and nothing changed
    assert rows["B"]["reason"] == "not_sent" and rows["C"]["reason"] == "not_sent"
    assert r["notes"][0]["code"] == "stopped"


def test_a_lowering_hktvmall_ignored_is_not_lowered():
    from conftest import FakeAccount, FakeSite, product

    site = FakeSite(lines={"A": 5}, products=[product("A")])
    real = FakeAccount.set_quantity
    FakeAccount.set_quantity = lambda self, sku, qty: None  # HKTVmall answers and changes nothing
    try:
        row = cart.update(site, [{"id": "A", "quantity": 2}], False)["changes"][0]
    finally:
        FakeAccount.set_quantity = real
    assert row["outcome"] == "rejected" and row["reason"] == "not_lowered"


def test_stock_limit_comes_from_the_basket_not_from_search():
    from conftest import FakeSite, product

    site = FakeSite(products=[product("A")], cap=3)  # the basket reports stockLevel 3; search has none
    row = cart.update(site, [{"id": "A", "quantity": 5}], False)["changes"][0]
    assert row["outcome"] == "adjusted" and row["reason"] == "stock_limit" and row["in_cart"] == 3


def test_a_search_outage_changes_nothing_instead_of_calling_everything_not_found():
    from conftest import FakeSite

    site = FakeSite(lines={"A": 1})
    site.search_up = False
    with pytest.raises(HktvError) as err:
        cart.update(site, [{"id": "A", "quantity": 3}, {"id": "NEW", "quantity": 1}], False)
    assert err.value.code == "upstream_error" and not [c for c in site.calls if c[0] in ("add", "set")]


def test_a_basket_line_missing_from_search_can_still_be_raised():
    from conftest import FakeSite

    site = FakeSite(lines={"A": 1}, products=[])  # search works but no longer lists A
    site.products = {"OTHER": {"code": "OTHER"}}
    row = cart.update(site, [{"id": "A", "quantity": 3}], False)["changes"][0]
    assert row["outcome"] == "applied"


def test_a_free_line_keeps_its_zero_price():
    data = load("checkout.json")
    entry = data["cartDataGroupByDeliveryDate"]["deliveryDateOrderEntries"][0]["orderEntries"][0]
    free = {**entry, "discountedUnitPrice": {"value": "0"}, "totalPrice": {"value": "0"}}
    data["cartDataGroupByDeliveryDate"]["deliveryDateOrderEntries"][0]["orderEntries"] = [free]
    line = cart.view(data)["line_items"][0]
    assert line["unit_price"]["cents"] == 0 and line["total"]["cents"] == 0


def test_basket_survives_unreadable_checkout_warnings(service, site):
    from conftest import FakeAccount

    real = FakeAccount.checkout_errors

    def broken(self):
        raise HktvError("schema_changed")

    FakeAccount.checkout_errors = broken
    try:
        view = service.basket()
    finally:
        FakeAccount.checkout_errors = real
    assert view.notes and view.notes[-1]["code"] == "warnings_unavailable"


def test_fakes_match_fixture_shapes():
    # The fakes once invented a stockLevel that HKTVmall's search hits never carry, which hid a dead code path.
    from conftest import product

    def keys(o, p=""):
        if isinstance(o, dict):
            for k, v in o.items():
                yield f"{p}.{k}"
                yield from keys(v, f"{p}.{k}")

    real = set().union(*(set(keys(h)) for h in load("search_hits.json")))
    assert set(keys(product("X"))) <= real, set(keys(product("X"))) - real


def test_one_field_name_one_shape_across_tool_outputs(service, site, clock):
    # "unit_price" once meant the item price in the basket and a per-100ml price in search.
    from conftest import product
    from tests_support import publish_snapshot

    from hktvmall_mcp import orders, views
    from hktvmall_mcp.server import Query

    hits = load("search_hits.json")
    site.products = {h["code"]: h for h in hits} | {"EGG": product("EGG", name="Eggs")}
    site.lines = {"EGG": 2}
    publish_snapshot(service.settings.snapshot_path, clock())
    outputs = [
        service.search([Query(q="milk")], "supermarket", 3).model_dump(),
        service.products([h["code"] for h in hits]).model_dump(),
        cart.view(load("checkout.json")),
        views.reviews({"stat": load("review_stat.json"), "page": load("reviews.json")}, 3),
        views.slots(load("delivery_slots.json")["data"]),
        orders.view(load("order_list.json")["closedPageData"]["results"][0], []),
    ]

    def shape(v):
        if isinstance(v, dict):
            return ("dict", tuple(sorted(v)))
        return "number" if isinstance(v, int | float) and not isinstance(v, bool) else type(v).__name__

    seen: dict[str, set] = {}

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if v is not None:
                    seen.setdefault(k, set()).add(shape(v))
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    for o in outputs:
        walk(o)
    clashes = {
        k: s
        for k, s in seen.items()
        if len({x[0] if isinstance(x, tuple) else x for x in s}) > 1 or len([x for x in s if isinstance(x, tuple)]) > 1
    }
    allowed = {"total"}  # money in totals and orders, a match count in a search result (documented)
    assert set(clashes) <= allowed, clashes


def test_a_line_hktvmall_drops_is_rejected_dropped_not_adjusted():
    from conftest import FakeAccount, FakeSite, product

    for have, want in ((2, 4), (5, 2)):
        site = FakeSite(lines={"A": have}, products=[product("A")])
        real = FakeAccount.set_quantity
        FakeAccount.set_quantity = lambda self, sku, qty: self.site.lines.pop(sku)
        try:
            row = cart.update(site, [{"id": "A", "quantity": want}], False)["changes"][0]
        finally:
            FakeAccount.set_quantity = real
        assert row["outcome"] == "rejected" and row["reason"] == "dropped" and row["in_cart"] == 0, (have, want)


def test_an_unpriced_product_is_never_added_as_free():
    from conftest import FakeSite

    site = FakeSite(products=[{"code": "TV", "nameEn": "TV", "stock": {"stockLevelStatus": {"code": "inStock"}}}])
    row = cart.update(site, [{"id": "TV", "quantity": 1}], False)["changes"][0]
    assert row["reason"] == "no_price" and not [c for c in site.calls if c[0] == "add"]
