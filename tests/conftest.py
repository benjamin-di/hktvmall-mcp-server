"""Shared fakes. Nothing here touches the network.

FakeSite reproduces HKTVmall behaviour observed on the live site (docs/hktvmall-site.md): an add that
silently drops an item, a silent stock cap, and the zero-quantity "zombie line" that makes every basket
read fail until the line is removed.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest
from tests_support import EMAIL

from hktvmall_mcp.config import Settings
from hktvmall_mcp.errors import HktvError
from hktvmall_mcp.server import HktvService
from hktvmall_mcp.session import SessionStore
from hktvmall_mcp.state import StateStore


def product(code: str, price: float = 10.0, stock: str = "inStock", name: str = "Item") -> dict:
    """A search hit shaped like tests/fixtures/search_hits.json: no stockLevel, which only the basket reports
    (test_fakes_match_fixture_shapes keeps it that way)."""
    return {
        "code": code,
        "nameEn": name,
        "sellingPrice": price,
        "priceList": [{"priceType": "BUY", "value": price}],
        "stock": {"stockLevelStatus": {"code": stock}},
    }


class Clock:
    def __init__(self, now: float = 1_792_300_000.0) -> None:  # 2026-10-18
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeAccount:
    def __init__(self, site: FakeSite) -> None:
        self.site = site

    def basket(self) -> dict[str, Any]:
        s = self.site
        s.basket_reads += 1
        if s.fail_read_after is not None and s.basket_reads > s.fail_read_after:
            raise HktvError("upstream_error", log_detail="scripted")
        if s.zombie:
            raise HktvError("basket_unreadable", log_detail="403 html")
        entries = [
            {
                "quantity": q,
                "product": {"code": sku, "name": sku, "stock": {"stockLevel": s.cap or 50}},
                "basePrice": {"value": "10.0"},
                "totalPrice": {"value": str(10.0 * q)},
            }
            for sku, q in s.lines.items()
        ]
        return {
            "cartDataGroupByDeliveryDate": {
                "deliveryDateOrderEntries": [
                    {
                        "batchNumber": 1,
                        "groupTitle": "Delivery Batch 1",
                        "deliveryMode": "hktv-standard-delivery",
                        "freeDelivery": False,
                        "orderEntries": entries,
                    }
                ]
            },
            "cartSubTotal": {"value": str(10.0 * sum(s.lines.values()))},
            "deliveryThreshold": "$ 288.00",
            "hktvDeliveryFee": {"value": {"value": "40.0"}},
            "deliveryCost": "$ 40.00",
        }

    def verify(self) -> None:
        self.site.calls.append(("verify",))

    def remint(self) -> None:
        self.site.calls.append(("remint",))

    def add(self, sku: str, qty: int) -> dict:
        self.site.calls.append(("add", sku, qty))
        if sku not in self.site.drop_on_add:
            self.site.lines[sku] = min(qty, self.site.cap or qty)
        return {"http": 200, "status": "success", "added": str(qty), "entry": False, "products": 0}  # it always says so

    def delivery_zone(self) -> str:
        self.site.calls.append(("delivery_zone",))
        return "ZONE_TEST_1"

    def delivery_slots(self, zone: str) -> dict:
        self.site.calls.append(("delivery_slots", zone))
        return self.site.slots

    def checkout_errors(self) -> list:
        return list(self.site.checkout_warnings)

    def order_list(self, page: int, processing_page: int = 0) -> dict:
        # Like HKTVmall: five of each kind a page, each kind with its own page counter.
        self.site.calls.append(("order_list", page, processing_page))
        done, busy = self.site.orders["completed"], self.site.orders["processing"]
        return {
            "completed": done[page * 5 : page * 5 + 5],
            "processing": busy[processing_page * 5 : processing_page * 5 + 5],
            "total": len(done),
            "processing_total": len(busy),
        }

    def order_entries(self, number: str) -> list:
        self.site.calls.append(("order_entries", number))
        return self.site.order_entries[number]

    def set_quantity(self, sku: str, qty: int) -> None:
        assert qty >= 1, "quantity 0 must never reach update_item_in_cart"
        self.site.calls.append(("set", sku, qty))
        self.site.lines[sku] = min(qty, self.site.cap or qty)

    def remove(self, sku: str) -> None:
        self.site.calls.append(("remove", sku))
        if sku in self.site.zombie_on_remove:
            self.site.zombie_on_remove.discard(sku)
            self.site.zombie.add(sku)  # first remove leaves a zombie; the repair removes it
            return
        self.site.lines.pop(sku, None)
        self.site.zombie.discard(sku)


class FakeSite:
    def __init__(self, lines=None, products=(), drop_on_add=(), cap=None, signed_in=True):
        self.lines: dict[str, int] = dict(lines or {})
        self.products = {p["code"]: p for p in products}
        self.drop_on_add, self.cap = set(drop_on_add), cap
        self.zombie: set[str] = set()
        self.zombie_on_remove: set[str] = set()
        self.fail_read_after: int | None = None
        self.basket_reads = 0
        self.signed_in = signed_in
        self.calls: list[tuple] = []
        self.searches: list[list[dict]] = []
        self.orders: dict[str, list] = {"completed": [], "processing": []}
        self.slots: dict = {  # the shape of tests/fixtures/delivery_slots.json, one slot
            "deliveryTimeSlots": [{"formattedValue": "5 Mar (Tue) 10:00AM - 2:00PM", "isNotAvailable": False}],
            "deliveryFee": 0.0,
            "deliveryThreshold": 88.0,
        }
        self.checkout_warnings: list = []
        self.review_pages: dict = {}
        self.search_up = True
        self.order_entries: dict[str, list] = {}

    @contextmanager
    def account(self):
        if not self.signed_in:
            raise HktvError("session_expired")
        yield FakeAccount(self)

    def search_works(self):
        self.calls.append(("search_works",))
        return self.search_up

    def reviews(self, product_id, page_size, rating):
        self.calls.append(("reviews", product_id, page_size, rating))
        return self.review_pages

    def lookup(self, ids):
        return {i: self.products[i] for i in ids if i in self.products}

    def search(self, queries):
        self.searches.append(queries)
        out = []
        for q in queries:
            if q.get("codes"):
                hits = [self.products[c] for c in q["codes"] if c in self.products]
            else:
                hits = [p for p in self.products.values() if q["q"].lower() in p["nameEn"].lower()]
                if q.get("zone") == "supermarket":
                    hits = [p for p in hits if not p.get("not_supermarket")]
            out.append({"total": len(hits), "hits": hits[: q.get("limit", 3)]})
        return out


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        snapshot_path=tmp_path / "source" / "snapshot.json", expected_email=EMAIL, state_dir=tmp_path / "state"
    )


@pytest.fixture
def site() -> FakeSite:
    return FakeSite(
        products=[
            product("EGG", 24.5, name="Fresh Eggs"),
            product("OAT", 25.9, name="Oat Milk"),
            {**product("BAT", 30.0, name="AA Batteries"), "not_supermarket": True},
        ]
    )


@pytest.fixture
def service(site, settings, clock) -> HktvService:
    state = StateStore(settings.state_dir, budget=100, clock=clock)
    session = SessionStore(settings.snapshot_path, settings.expected_email, clock=clock)
    return HktvService(site, state, session, settings)
