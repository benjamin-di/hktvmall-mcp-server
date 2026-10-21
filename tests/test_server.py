"""The MCP contract through an in-process client: tool list, annotations, schemas, results, errors."""

import asyncio
import json

from fastmcp import Client

from hktvmall_mcp import server
from hktvmall_mcp.accounts import Accounts

TOOLS = {
    "hktv_search",
    "hktv_get_products",
    "hktv_show_products",
    "hktv_get_cart",
    "hktv_update_cart",
    "hktv_recent_orders",
    "hktv_delivery_slots",
    "hktv_reviews",
    "hktv_status",
}


def run(coro):
    return asyncio.run(coro)


def test_tool_contracts(service):
    async def go():
        async with Client(server.create_server(service)) as c:
            return {t.name: t for t in await c.list_tools()}

    tools = run(go())
    assert set(tools) == TOOLS
    for t in tools.values():
        assert t.title and t.description and t.annotations
        assert t.annotations.read_only_hint is not None and t.annotations.destructive_hint is False
        for name, prop in t.input_schema.get("properties", {}).items():
            assert prop.get("description") or "$ref" in json.dumps(prop), (t.name, name)
    assert tools["hktv_update_cart"].annotations.read_only_hint is False
    assert tools["hktv_update_cart"].annotations.idempotent_hint is True
    for name in ("hktv_search", "hktv_get_cart", "hktv_update_cart"):
        assert (tools[name].meta or {}).get("anthropic/alwaysLoad") is True
    for name in TOOLS - {"hktv_status"}:
        assert tools[name].output_schema and tools[name].output_schema.get("additionalProperties") is not False


def test_search_batches_widens_and_prices(service, site):
    async def go():
        async with Client(server.create_server(service)) as c:
            return (
                await c.call_tool("hktv_search", {"queries": [{"q": "eggs"}, {"q": "batteries"}]})
            ).structured_content

    res = run(go())["results"]
    assert res[0]["products"][0]["price"] == {"cents": 2450, "display": "HK$24.50"}
    assert res[0]["widened"] is False and res[1]["widened"] is True and res[1]["products"][0]["id"] == "BAT"
    assert len(site.searches[0]) == 2  # the whole list in one HKTVmall request


def test_update_reports_outcomes_and_basket(service):
    async def go():
        async with Client(server.create_server(service)) as c:
            return (
                await c.call_tool("hktv_update_cart", {"changes": [{"id": "EGG", "quantity": 2}]})
            ).structured_content

    out = run(go())
    assert out["status"] == "ok" and out["changes"][0]["outcome"] == "applied"
    assert out["basket"]["checkout_url"].startswith("https://www.hktvmall.com/")


def test_errors_are_typed_and_actionable(service, site):
    site.signed_in = False

    async def go():
        async with Client(server.create_server(service)) as c:
            dup = await c.call_tool(
                "hktv_update_cart", {"changes": [{"id": "EGG", "quantity": 1}] * 2}, raise_on_error=False
            )
            big = await c.call_tool(
                "hktv_update_cart", {"changes": [{"id": "EGG", "quantity": 500}]}, raise_on_error=False
            )
            cart = await c.call_tool("hktv_get_cart", {}, raise_on_error=False)
            return dup, big, cart

    dup, big, basket = run(go())
    for result, code in ((big, "invalid_argument"), (basket, "session_expired")):
        assert result.is_error and json.loads(result.content[0].text)["error"]["code"] == code
    assert "changes.0.quantity" in json.loads(big.content[0].text)["error"]["next_step"]
    assert dup.is_error  # the expired sign-in comes first here; duplicates are covered in test_cart


def test_status_and_canary(service, site, clock):
    status = service.status(check=True)
    assert status["signed_in"] is False and status["canary"]["code"] == "session_source_missing"
    assert status["session_source"] == {"state": "missing", "generation": None}
    assert service.status(check=True)["canary"]["cached"] is True
    assert status["budget"]["limit"] == 100


def test_canary_states(service, site, clock):
    import json as _json

    from tests_support import publish_snapshot

    publish_snapshot(service.settings.snapshot_path, clock())
    calls = []
    service.keepalive = lambda: calls.append("keepalive")
    first = service.canary()
    assert first["ok"] is True and calls == ["keepalive"] and first["generation"] == 1
    assert service.canary()["cached"] is True and calls == ["keepalive"]
    service.session.mark("dead", 1)
    clock.now += 56 * 60
    assert service.canary()["code"] == "session_expired"
    publish_snapshot(service.settings.snapshot_path, clock(), 2)  # a new generation is checked at once
    assert service.canary()["cached"] is False
    service.settings.snapshot_path.unlink()
    assert service.canary()["code"] == "session_source_missing"
    assert _json.dumps(service.status(check=False))  # serialisable


def test_last_error_clears_when_the_tool_next_succeeds(service, site, clock):
    from tests_support import publish_snapshot

    async def cart():
        async with Client(server.create_server(service)) as c:
            return await c.call_tool("hktv_get_cart", {}, raise_on_error=False)

    site.signed_in = False
    assert run(cart()).is_error
    assert service.status(check=False)["last_error"]["tool"] == "hktv_get_cart"
    assert service.status(check=False)["last_error"]["code"] == "session_expired"

    async def bad_args():
        async with Client(server.create_server(service)) as c:
            return await c.call_tool(
                "hktv_update_cart", {"changes": [{"id": "EGG", "quantity": 500}]}, raise_on_error=False
            )

    clock.now += 1
    assert run(bad_args()).is_error  # an agent's bad arguments are not a service error
    assert service.status(check=False)["last_error"]["tool"] == "hktv_get_cart"

    site.signed_in = True
    publish_snapshot(service.settings.snapshot_path, clock())
    assert not run(cart()).is_error
    assert service.status(check=False)["last_error"] is None


def test_keepalive_disproves_session_failures_only(service, site, clock):
    import pytest

    from hktvmall_mcp.errors import HktvError

    site.signed_in = False
    with pytest.raises(HktvError):
        service.keepalive()
    assert service.status(check=False)["last_error"]["tool"] == "keepalive"
    service.state.fail("hktv_update_cart", "session_expired")
    service.state.fail("hktv_search", "schema_changed")
    site.signed_in = True
    service.keepalive()
    assert service.status(check=False)["last_error"]["tool"] == "hktv_search"


def test_legacy_and_unknown_errors_are_dropped_on_load(settings, clock):
    from hktvmall_mcp.state import StateStore

    settings.state_dir.mkdir(parents=True, exist_ok=True)
    (settings.state_dir / "state.json").write_text(
        json.dumps(
            {
                "last_error": {"at": "2030-01-02T03:04:05+00:00", "code": "not_signed_in"},  # an early release's format
                "errors": {
                    "hktv_get_cart": {"code": "not_signed_in", "at": 1.0},
                    "hktv_search": {"code": "busy", "at": 2.0},
                },
            }
        )
    )
    state = StateStore(settings.state_dir, budget=100, clock=clock)
    assert state.snapshot()["last_error"] == {"code": "busy", "at": "1970-01-01T00:00:02+00:00", "tool": "hktv_search"}


def test_keepalive_proves_a_new_generation_at_once_then_keeps_its_interval(service, clock):
    import threading

    from tests_support import publish_snapshot

    calls = []

    def keepalive():
        calls.append(service.session.generation)
        service.session.mark("live", service.session.generation)
        service.state.record("last_keepalive_at", clock())

    service.keepalive = keepalive
    stopped = threading.Event()
    stopped.set()  # one pass of the loop, then stop

    service.state.record("last_keepalive_at", clock())  # a restart: recent keep-alive, unproven session
    publish_snapshot(service.settings.snapshot_path, clock())
    assert service.status(check=False)["signed_in"] is False
    server._keepalive_loop(Accounts(service.settings, None, service), stopped)
    assert calls == [1] and service.status(check=False)["signed_in"] is True

    clock.now += 60
    # live and inside the interval: nothing to do
    server._keepalive_loop(Accounts(service.settings, None, service), stopped)
    assert calls == [1]

    publish_snapshot(service.settings.snapshot_path, clock(), 2)  # the session publisher's next snapshot
    server._keepalive_loop(Accounts(service.settings, None, service), stopped)
    assert calls == [1, 2] and service.status(check=False)["generation"] == 2

    clock.now += service.settings.keepalive_seconds
    server._keepalive_loop(Accounts(service.settings, None, service), stopped)
    assert calls == [1, 2, 2]


def test_failed_keepalive_waits_before_retrying(service, site, clock):
    from tests_support import publish_snapshot

    publish_snapshot(service.settings.snapshot_path, clock())
    site.signed_in = False
    assert service.keepalive_due(None)
    attempted = clock()
    clock.now += server.KEEPALIVE_RETRY - 1
    assert not service.keepalive_due(attempted)
    clock.now += 1
    assert service.keepalive_due(attempted)


def test_search_marks_a_closed_store_with_its_reopen_time():
    from conftest import product as raw

    closed = {
        **raw("OIL"),
        "storeClosedStatus": 1,
        "deliveryLabelScheduleCode": [{"endTime": 4_102_444_800_000, "deliveryMode": "closed-store"}],
    }
    view = server.product(closed)
    assert view["availability"] == "store_closed" and view["reopens_at"] == "2100-01-01T08:00+08:00"
    assert "reopens_at" not in server.product(raw("EGG"))


def _order(number, status="completed"):
    return {"code": number, "placed": "2030-03-01T10:00:00+08:00", "statusDisplay": status, "total": {"value": 70.0}}


def _entry(code, qty, paid):
    return {
        "product": {"code": code},
        "productName": code.title(),
        "quantity": qty,
        "entryPrice": paid,
        "statusCode": "ORDER_COMPLETE",
    }


def test_recent_orders_pages_newest_first_and_caches_completed_orders(service, site):
    site.orders = {
        "processing": [_order("H0000000099", "processing")],
        "completed": [_order(f"H00000000{n:02d}") for n in range(12)],
    }
    site.order_entries = {
        o["code"]: [_entry("EGG", 2, "$ 49.00"), _entry("GIFT", 1, "FREE")]
        for o in site.orders["processing"] + site.orders["completed"]
    }
    first = service.recent_orders(7)
    assert [o["order"] for o in first.orders][:2] == ["H0000000099", "H0000000000"] and len(first.orders) == 7
    assert first.total_orders == 13 and ("order_list", 1, 0) in site.calls and ("order_list", 2, 0) not in site.calls
    items = first.orders[1]["items"]
    assert items[0] == {
        "id": "EGG",
        "title": "Egg",
        "quantity": 2,
        "line_total": {"cents": 4900, "display": "HK$49.00"},
    }
    assert items[1]["free_gift"] is True and first.orders[1]["total"]["display"] == "HK$70.00"
    reads = sum(1 for c in site.calls if c[0] == "order_entries")
    service.recent_orders(7)
    again = sum(1 for c in site.calls if c[0] == "order_entries") - reads
    assert again == 1  # only the processing order is read again; completed orders never change


def test_recent_orders_tool_is_read_only_and_needs_the_sign_in(service, site):
    site.signed_in = False

    async def go():
        async with Client(server.create_server(service)) as c:
            tools = {t.name: t for t in await c.list_tools()}
            result = await c.call_tool("hktv_recent_orders", {"limit": 2}, raise_on_error=False)
            return tools["hktv_recent_orders"], result

    tool, result = run(go())
    assert tool.annotations.read_only_hint is True
    assert result.is_error and json.loads(result.content[0].text)["error"]["code"] == "session_expired"


def test_a_search_where_every_item_is_empty_says_so(service, site, caplog):
    from hktvmall_mcp.server import Query

    site.products = {}
    assert service.search([Query(q="nothing like this")], "supermarket", 3).notes is None  # the probe has hits
    site.search_up = False  # the probe is empty too: an outage
    r = service.search([Query(q="toilet paper")], "supermarket", 3)
    assert r.notes and r.notes[0]["code"] == "search_empty" and "retry" in r.notes[0]["text"]
    assert "came back empty and so did the probe" in caplog.text
    p = service.products(["EGG"])
    assert p.not_found == ["EGG"] and p.notes[0]["code"] == "search_empty"


def test_a_successful_keepalive_leaves_a_new_generation_due_at_once(service, clock):
    from tests_support import publish_snapshot

    service.keepalive = lambda: (
        service.session.mark("live", service.session.generation),
        service.state.record("last_keepalive_at", clock()),
    )
    publish_snapshot(service.settings.snapshot_path, clock())
    import threading

    stop = threading.Event()
    stop.set()
    server._keepalive_loop(Accounts(service.settings, None, service), stop)
    assert service.status(check=False)["signed_in"] is True
    clock.now += 60
    publish_snapshot(service.settings.snapshot_path, clock(), 2)
    assert service.keepalive_due(None)  # no failure, so nothing delays proving generation 2


def test_total_orders_counts_every_processing_order(service, site):
    site.orders = {
        "processing": [_order(f"H00000001{n:02d}", "processing") for n in range(7)],
        "completed": [_order("H0000000001")],
    }
    site.order_entries = {o["code"]: [] for o in site.orders["processing"] + site.orders["completed"]}
    assert service.recent_orders(1).total_orders == 8


def test_processing_orders_are_paged_before_completed_ones(service, site):
    site.orders = {
        "processing": [_order(f"H00000002{n:02d}", "processing") for n in range(7)],
        "completed": [_order(f"H00000003{n:02d}") for n in range(8)],
    }
    site.order_entries = {o["code"]: [] for o in site.orders["processing"] + site.orders["completed"]}
    got = service.recent_orders(10)
    assert [o["status"] for o in got.orders] == ["processing"] * 7 + ["completed"] * 3
    assert got.total_orders == 15


def test_the_daily_deep_check_alerts_through_the_canary_and_names_the_area(service, site, clock):
    from tests_support import publish_snapshot

    publish_snapshot(service.settings.snapshot_path, clock())
    service.keepalive = lambda: None
    first = service.canary()
    assert first["ok"] and first["deep"]["ok"] and first["deep"]["cached"] is False
    assert service.canary()["deep"]["cached"] is True  # once a day, not every probe
    # an HKTVmall change on the order page, found by the next daily run
    clock.now += server.DEEP_TTL
    from conftest import FakeAccount

    from hktvmall_mcp.errors import HktvError

    real = FakeAccount.order_list

    def broken(self, page, processing_page=0):
        raise HktvError("schema_changed")

    FakeAccount.order_list = broken
    try:
        failed = service.canary()
    finally:
        FakeAccount.order_list = real
    assert failed["ok"] is False and failed["code"] == "orders_schema_changed"
    assert failed["deep"]["failures"] == ["orders_schema_changed"]
    clock.now += server.DEEP_RETRY  # re-checked sooner after a failure, and recovers
    assert service.canary()["ok"] is True


def test_the_deep_check_waits_for_the_hourly_one(service, site, clock):
    calls = []
    service._deep_canary = lambda: calls.append("deep") or {"ok": True}
    assert service.canary()["code"] == "session_source_missing" and calls == []


def test_one_search_call_is_capped_at_100_products(service, site):
    from conftest import product

    from hktvmall_mcp.server import Query

    site.products = {f"P{i}": product(f"P{i}", name=f"milk {i}") for i in range(20)}
    r = service.search([Query(q="milk") for _ in range(25)], "supermarket", 10)
    assert all(len(x["products"]) == 4 for x in r.results) and r.notes[0]["code"] == "per_query_reduced"
    assert service.search([Query(q="milk")], "supermarket", 10).notes is None  # 10 is within the cap


def test_an_empty_basket_has_no_slots_by_design_and_the_deep_check_passes(service, site, clock):
    from tests_support import publish_snapshot

    from hktvmall_mcp.site import NO_SLOTS

    site.slots = {"deliveryFee": 0.0, "deliveryThreshold": 0.0, NO_SLOTS: True}
    v = service.delivery_slots()
    assert v.slots == [] and v.notes[0]["code"] == "basket_empty"
    assert v.fee is None and v.free_from is None  # not HKTVmall's 0.0, which reads as free delivery
    publish_snapshot(service.settings.snapshot_path, clock())
    service.keepalive = lambda: None
    assert service.canary()["deep"]["ok"] is True
    # the same answer with items in the basket is not believed
    site.lines = {"A": 1}
    clock.now += server.DEEP_TTL
    failed = service.canary()
    assert failed["ok"] is False and failed["deep"]["failures"] == ["slots_schema_changed"]


def test_show_products_draws_the_agents_picks_as_cards(service, site):
    from conftest import product

    site.products["NAP"] = {
        **product("NAP", 199, name="Linen-Feel Napkins"),
        "urlEn": "main/Shop/s/S1/Housewares/Napkins/Linen-Feel-Napkins/p/NAP",
        "imageLink": "https://images.hktv-img.com/images/HKTV/1/NAP_300.jpg",
        "gallery": [
            {
                "imageType": "PRIMARY",
                "format": "product",
                "url": "https://images.hktv-img.com/images/HKTV/1/NAP_1200.jpg",
            },
            {"imageType": "GALLERY", "format": "product", "url": "https://cdn-media.hktvmall.com/x/close-up_1200.jpg"},
        ],
    }
    picks = [{"id": "NAP", "note": " For dinner "}, {"id": "EGG"}, {"id": "NAP", "note": "again"}, {"id": "GONE"}]

    async def go():
        async with Client(server.create_server(service)) as c:
            tools = {t.name: t for t in await c.list_tools()}
            shown = await c.call_tool("hktv_show_products", {"items": picks, "title": "Napkins"})
            [cards] = await c.read_resource(server.CARDS_URI)
            listed = {str(r.uri): r for r in await c.list_resources()}
            return tools["hktv_show_products"], shown, cards, listed

    tool, result, cards, listed = run(go())
    shown = result.structured_content
    # The gallery reaches the cards in _meta and stays out of what the model reads.
    assert result.meta[server.PHOTOS_META] == {
        "NAP": [
            "https://images.hktv-img.com/images/HKTV/1/NAP_515.jpg",
            "https://cdn-media.hktvmall.com/x/close-up_515.jpg",
        ],
        "EGG": [],
    }
    assert "gallery" not in json.dumps(shown) and json.loads(result.content[0].text) == shown
    # Only the metadata of views claude.ai is known to render: no legacy ui/resourceUri, no alwaysLoad.
    assert set(tool.meta) == {"ui", "fastmcp"}
    assert tool.meta["ui"] == {"resourceUri": server.CARDS_URI, "visibility": ["model", "app"]}
    assert tool.annotations.read_only_hint is True and tool.output_schema["properties"]["products"]
    assert [p["id"] for p in shown["products"]] == ["NAP", "EGG"] and shown["not_found"] == ["GONE"]
    nap, egg = shown["products"]
    assert nap["note"] == "For dinner" and "note" not in egg  # the first note for an id; blank notes are dropped
    assert nap["url"] == "https://www.hktvmall.com/hktv/en/main/Shop/s/S1/Housewares/Napkins/Linen-Feel-Napkins/p/NAP"
    assert nap["image"].startswith("https://images.hktv-img.com/") and shown["title"] == "Napkins"
    assert egg["url"] == "https://www.hktvmall.com/hktv/en/p/EGG"  # no urlEn: HKTVmall's redirect, never nothing
    assert cards.mime_type == "text/html;profile=mcp-app" and server.CARDS_URI in listed
    assert cards.meta["ui"]["csp"] == server.CARDS_CSP and cards.meta["ui"]["prefersBorder"] is False
    assert "ui/notifications/tool-result" in cards.text and "__VERSION__" not in cards.text


def test_show_products_bounds_its_input(service):
    async def go(args):
        async with Client(server.create_server(service)) as c:
            return await c.call_tool("hktv_show_products", args, raise_on_error=False)

    for args in (
        {"items": []},
        {"items": [{"id": f"P{i}"} for i in range(server.MAX_SHOWN + 1)]},
        {"items": [{"id": "EGG", "note": "x" * 161}]},
        {"items": [{"id": "../cart"}]},
    ):
        r = run(go(args))
        assert r.is_error and json.loads(r.content[0].text)["error"]["code"] == "invalid_argument", args


def test_reviews_use_the_products_review_key(service, site):
    from conftest import product

    site.products["H9990007_S_10_Grains_Rice_1000g"] = {
        **product("H9990007_S_10_Grains_Rice_1000g"),
        "baseProduct": "H999000710_grains_family",
    }
    service.reviews("H9990007_S_10_Grains_Rice_1000g", 2, None)
    service.reviews("H9990008_S_A1234-B-CD50", 2, 1)  # hyphens are ordinary; not listed now: the id rule
    reviews = [c for c in site.calls if c[0] == "reviews"]
    assert reviews == [("reviews", "H999000710_grains_family", 2, None), ("reviews", "H9990008A1234-B-CD50", 2, 1)]


def _history(site, processing=1, completed=73):
    site.orders = {
        "processing": [_order(f"H1000000{n:03d}", "processing") for n in range(processing)],
        "completed": [_order(f"H2000000{n:03d}") for n in range(completed)],
    }
    site.order_entries = {
        o["code"]: [_entry("EGG", 1, "$ 24.50")] for o in site.orders["processing"] + site.orders["completed"]
    }
    return [o["code"] for o in site.orders["processing"] + site.orders["completed"]]


def test_the_whole_order_history_pages_back_with_offset(service, site):
    # The tool once showed the 10 newest orders of a long history and no more.
    expected = _history(site)
    seen, offset = [], 0
    while offset is not None:
        page = service.recent_orders(10, offset)
        assert page.total_orders == 74 and all("items" in o for o in page.orders)
        seen += [o["order"] for o in page.orders]
        offset = page.next_offset
    assert seen == expected
    assert sorted(c[1] for c in site.calls if c[0] == "order_entries") == sorted(expected)  # each order's page once


def test_order_summaries_list_fifty_without_reading_order_pages(service, site):
    expected = _history(site)
    first = service.recent_orders(50, 0, items=False)
    rest = service.recent_orders(50, first.next_offset, items=False)
    assert [o["order"] for o in first.orders + rest.orders] == expected and rest.next_offset is None
    assert "items" not in first.orders[0] and first.orders[0]["total"]["display"] == "HK$70.00"
    assert not [c for c in site.calls if c[0] == "order_entries"]
    reads = [c for c in site.calls if c[0] == "order_list"]
    # The order list only: the first call reads completed pages 0-9 (with the processing page), the second the
    # first page again (for the totals) and pages 9-14; no page twice within a call.
    assert len(reads) == 10 + 7 and len(set(reads[:10])) == 10 and len(set(reads[10:])) == 7
    # Items for one order deep in the history, by its position:
    one = service.recent_orders(1, 60)
    assert one.orders[0]["order"] == expected[60] and one.orders[0]["items"][0]["id"] == "EGG"


def test_order_items_are_read_ten_new_pages_a_call_and_kept_across_restarts(service, site, settings, clock):
    from hktvmall_mcp.server import HktvService
    from hktvmall_mcp.session import SessionStore
    from hktvmall_mcp.state import StateStore

    expected = _history(site)  # 1 processing + 73 completed

    def pages_read():
        return [c[1] for c in site.calls if c[0] == "order_entries"]

    first = service.recent_orders(50)
    assert len(first.orders) == 10 and first.next_offset == 10 and first.notes[0]["code"] == "more_to_read"
    offset = first.next_offset
    while offset is not None:
        offset = service.recent_orders(50, offset).next_offset
    assert sorted(pages_read()) == sorted(expected)  # every order's page once

    site.calls.clear()
    again = service.recent_orders(50)
    assert len(again.orders) == 50 and all("items" in o for o in again.orders) and again.notes is None
    assert pages_read() == [expected[0]]  # only the processing order, which can still change

    # A restart (a deploy) keeps them: orders.json beside the account's state.
    site.calls.clear()
    state = StateStore(settings.state_dir, budget=100, clock=clock)
    restarted = HktvService(
        site, state, SessionStore(settings.snapshot_path, settings.expected_email, clock=clock), settings
    )
    assert len(restarted.recent_orders(50, 24).orders) == 50 and pages_read() == []
    assert service.recent_orders(5, 74).orders == [] and service.recent_orders(5, 69).next_offset is None


def test_an_unreadable_order_cache_is_ignored(service, site, settings, clock):
    from hktvmall_mcp import orders

    path = settings.state_dir / "orders.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    for bad in ("{not json", '{"orders": [1]}', '{"orders": {"H1": [{"address": "x"}]}}'):
        path.write_text(bad, encoding="utf-8")
        assert orders.ItemCache(path).get("H1") is None  # never more than item() writes
