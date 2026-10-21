"""The update flow against HKTVmall's observed misbehaviour, and the basket view."""

import pytest
from conftest import FakeAccount, FakeSite, product

from hktvmall_mcp import cart
from hktvmall_mcp.errors import HktvError


def test_applied_and_lines_not_named_are_untouched():
    site = FakeSite(lines={"A": 1, "HUMAN": 2}, products=[product("A"), product("B")])
    r = cart.update(site, [{"id": "A", "quantity": 3}, {"id": "B", "quantity": 1}], confirmed=False)
    assert r["status"] == "ok" and site.lines == {"A": 3, "HUMAN": 2, "B": 1}


def test_adds_verify_once_and_remint_once():
    site = FakeSite(products=[product("A"), product("B")])
    cart.update(site, [{"id": "A", "quantity": 1}, {"id": "B", "quantity": 2}], confirmed=False)
    assert site.calls == [("verify",), ("add", "A", 1), ("add", "B", 2), ("remint",)]


def test_silent_drop_is_rejected_not_reported_as_added():
    site = FakeSite(products=[product("A")], drop_on_add={"A"})
    r = cart.update(site, [{"id": "A", "quantity": 1}], confirmed=False)
    assert r["status"] == "partial"
    assert r["changes"][0] == {"id": "A", "requested": 1, "in_cart": 0, "outcome": "rejected", "reason": "not_accepted"}


def test_silent_cap_is_adjusted():
    site = FakeSite(lines={"A": 2}, products=[product("A")], cap=5)
    r = cart.update(site, [{"id": "A", "quantity": 8}], confirmed=False)
    assert r["changes"][0]["outcome"] == "adjusted" and r["changes"][0]["reason"] == "stock_limit"


def test_out_of_stock_and_unknown_ids_are_never_sent():
    site = FakeSite(products=[product("OOS", stock="stockNotifiable")])
    r = cart.update(site, [{"id": "OOS", "quantity": 1}, {"id": "GONE", "quantity": 1}], confirmed=False)
    assert [c["reason"] for c in r["changes"]] == ["out_of_stock", "not_found"]
    assert not [c for c in site.calls if c[0] in ("add", "set", "remove")]


def test_zero_goes_through_remove_never_update():
    site = FakeSite(lines={"A": 2, "B": 1}, products=[product("A"), product("B")])
    cart.update(site, [{"id": "A", "quantity": 0}, {"id": "B", "quantity": 3}], confirmed=False)
    assert ("remove", "A") in site.calls and ("set", "B", 3) in site.calls and site.lines == {"B": 3}


def test_zombie_line_is_repaired_and_other_lines_are_never_removed():
    site = FakeSite(lines={"A": 2, "B": 1}, products=[product("A"), product("B")])
    site.zombie_on_remove = {"A"}
    r = cart.update(site, [{"id": "A", "quantity": 0}, {"id": "B", "quantity": 2}], confirmed=False)
    assert [c["outcome"] for c in r["changes"]] == ["applied", "applied"]
    assert site.calls.count(("remove", "A")) == 2 and ("remove", "B") not in site.calls and site.lines == {"B": 2}


def test_caps_refuse_without_writing_until_confirmed():
    site = FakeSite(products=[product("A", price=200.0)])
    assert cart.update(site, [{"id": "A", "quantity": 16}], False)["status"] == "requires_buyer_review"
    assert cart.update(site, [{"id": "A", "quantity": 8}], False)["status"] == "requires_buyer_review"  # HK$1,600
    assert not [c for c in site.calls if c[0] == "add"]
    assert cart.update(site, [{"id": "A", "quantity": 8}], True)["status"] == "ok"
    cheap = FakeSite(products=[product("C", price=10.0)])
    assert cart.update(cheap, [{"id": "C", "quantity": 15}], False)["status"] == "ok"  # the cap itself is allowed


def test_unreadable_after_write_is_outcome_unknown():
    site = FakeSite(products=[product("A")])
    site.fail_read_after = 1
    with pytest.raises(HktvError) as err:
        cart.update(site, [{"id": "A", "quantity": 1}], confirmed=False)
    assert err.value.code == "outcome_unknown"


def test_duplicate_ids_are_an_argument_error():
    with pytest.raises(HktvError) as err:
        cart.update(FakeSite(products=[product("A")]), [{"id": "A", "quantity": 1}, {"id": "A", "quantity": 2}], False)
    assert err.value.code == "invalid_argument"


def test_view_of_a_multi_group_basket():
    data = {  # shaped like HKTVmall's checkout payload for a basket in two delivery groups; invented values
        "cartDataGroupByDeliveryDate": {
            "deliveryDateOrderEntries": [
                {
                    "batchNumber": 1,
                    "groupTitle": "Delivery Batch 1",
                    "deliveryMode": "hktv-standard-delivery",
                    "freeDelivery": False,
                    "orderEntries": [
                        {
                            "quantity": 3,
                            "basePrice": {"value": "38.0"},
                            "discountedUnitPrice": {"value": "25.0"},
                            "totalPrice": {"value": "75.0"},
                            "product": {
                                "code": "P1",
                                "name": "Example Rice Crackers 300g",
                                "stock": {"stockLevel": 34},
                            },
                        },
                        {
                            "quantity": 1,
                            "basePrice": {"value": "52.0"},
                            "discountedUnitPrice": {"value": "52.0"},
                            "totalPrice": {"value": "52.0"},
                            "product": {"code": "P2", "name": "Example Tissue Box x 4", "stock": {"stockLevel": 28}},
                        },
                    ],
                },
                {
                    "batchNumber": 2,
                    "groupTitle": "Delivery Batch 2",
                    "deliveryMode": "merchant-delivery",
                    "freeDelivery": True,
                    "orderEntries": [
                        {
                            "quantity": 1,
                            "basePrice": {"value": "100.0"},
                            "discountedUnitPrice": {"value": "55.0"},
                            "totalPrice": {"value": "55.0"},
                            "product": {"code": "P3", "name": "Example Dish Soap 500ml", "stock": {"stockLevel": 3}},
                        }
                    ],
                },
            ]
        },
        "cartSubTotal": {"value": "182.00"},
        "deliveryThreshold": "$ 288.00",
        "deliveryCost": "$ 40.00",
        "hktvDeliveryFee": {"value": {"value": "40.0"}},
        "merchantDeliveryFeeList": [{"fee": {"value": "0.0"}, "storeCode": "S9990009"}],
    }
    v = cart.view(data)
    assert [line["available_quantity"] for line in v["line_items"]] == [34, 28, 3]
    assert (
        v["line_items"][0]["unit_price"]["display"] == "HK$25.00" and v["line_items"][0]["list_price"]["cents"] == 3800
    )
    g1, g2 = v["delivery_groups"]
    assert g1["amount_to_free_delivery"]["display"] == "HK$161.00" and g1["delivery_fee"]["cents"] == 4000
    assert "amount_to_free_delivery" not in g2
    assert v["totals"]["total"]["display"] == "HK$222.00"


def test_empty_basket_view():
    v = cart.view({"redirectPage": "/cart"})
    assert v["line_items"] == [] and v["totals"]["total"]["cents"] == 0


def test_failure_part_way_reports_what_took_effect_instead_of_an_error():
    site = FakeSite(products=[product("A"), product("B")])
    real_add = FakeAccount.add

    def flaky_add(self, sku, qty):
        if sku == "B":
            raise HktvError("upstream_error", log_detail="network")
        real_add(self, sku, qty)

    FakeAccount.add = flaky_add
    try:
        r = cart.update(site, [{"id": "A", "quantity": 1}, {"id": "B", "quantity": 1}], confirmed=False)
    finally:
        FakeAccount.add = real_add
    assert r["status"] == "partial" and [c["outcome"] for c in r["changes"]] == ["applied", "rejected"]
    assert r["notes"][0]["code"] == "stopped" and "upstream_error" in r["notes"][0]["text"]


def test_low_stock_is_still_buyable():
    site = FakeSite(products=[product("LOW", stock="lowStock")])
    r = cart.update(site, [{"id": "LOW", "quantity": 1}], confirmed=False)
    assert r["changes"][0]["outcome"] == "applied" and ("add", "LOW", 1) in site.calls


def test_lowering_a_line_above_the_cap_needs_no_confirmation():
    site = FakeSite(lines={"A": 20}, products=[product("A")])
    r = cart.update(site, [{"id": "A", "quantity": 15}], confirmed=False)
    assert r["status"] == "ok" and site.lines == {"A": 15}
    assert cart.update(site, [{"id": "A", "quantity": 16}], False)["status"] == "requires_buyer_review"


def test_free_delivery_basket_charges_no_flat_delivery_cost_and_adds_listed_fees():
    data = {
        "cartDataGroupByDeliveryDate": {
            "deliveryDateOrderEntries": [
                {
                    "batchNumber": 1,
                    "deliveryMode": "hktv-standard-delivery",
                    "freeDelivery": True,
                    "orderEntries": [
                        {
                            "quantity": 2,
                            "basePrice": {"value": "180.0"},
                            "discountedUnitPrice": {"value": "144.0"},
                            "totalPrice": {"value": "288.0"},
                            "product": {"code": "W"},
                        },
                        {
                            "quantity": 1,
                            "basePrice": {"value": "98.0"},
                            "discountedUnitPrice": {"value": "79.5"},
                            "totalPrice": {"value": "79.5"},
                            "product": {"code": "V"},
                        },
                    ],
                }
            ]
        },
        # As HKTVmall sends it (tests/fixtures/checkout.json): deliveryCost stays at HK$40 although the basket
        # delivers free.
        "cartSubTotal": {"value": "367.50"},
        "deliveryThreshold": "$ 88.00",
        "deliveryCost": "$ 40.00",
        "hktvDeliveryFee": {"value": {"currencyIso": "HKD", "value": "0"}},
        "merchantDeliveryFeeList": [],
        "otherFeeRemarks": [
            {"code": "plasticBagLevy", "fee": {"value": "1.0"}},
            {"code": "fuelSurcharge", "fee": {"value": "1.1"}},
        ],
    }
    totals = cart.view(data)["totals"]
    assert totals["delivery_fee"]["cents"] == 0
    assert totals["other_fees"]["display"] == "HK$2.10"
    assert totals["total"]["display"] == "HK$369.60"


def test_closed_store_items_are_refused_before_sending_with_the_reopen_time(monkeypatch):
    reopen_ms = 1_898_830_800_000  # 2030-03-04 13:00 HKT
    monkeypatch.setattr(cart.time, "time", lambda: 1_898_740_800.0)  # inside the window, whatever today is
    closed = {
        **product("OIL", price=98.0),
        "storeClosedStatus": 1,
        "deliveryLabelScheduleCode": [
            {"startTime": 1_898_647_200_000, "endTime": reopen_ms, "deliveryMode": "closed-store"},
            {"startTime": reopen_ms, "endTime": reopen_ms + 3_600_000, "deliveryMode": "standard-express-slow"},
        ],
    }
    site = FakeSite(products=[closed, product("EGG")])
    r = cart.update(site, [{"id": "OIL", "quantity": 1}, {"id": "EGG", "quantity": 1}], False)
    oil, egg = r["changes"]
    assert oil == {
        "id": "OIL",
        "requested": 1,
        "in_cart": 0,
        "outcome": "rejected",
        "reason": "store_closed",
        "reopens_at": "2030-03-04T13:00+08:00",
    }
    assert egg["outcome"] == "applied" and r["status"] == "partial"
    assert ("add", "OIL", 1) not in site.calls
    assert cart.update(site, [{"id": "OIL", "quantity": 0}], False)["changes"][0]["outcome"] == "applied"


def test_reopen_time_is_unknown_without_a_future_closed_window():
    assert cart.reopens_at({"storeClosedStatus": 1}) is None
    window = {"endTime": 1_000, "deliveryMode": "closed-store"}
    assert cart.reopens_at({"deliveryLabelScheduleCode": [window]}, now=2.0) is None


def test_total_is_hktvmalls_total_payment_when_present(caplog):
    data = {
        "cartDataGroupByDeliveryDate": {
            "deliveryDateOrderEntries": [
                {
                    "batchNumber": 1,
                    "deliveryMode": "hktv-standard-delivery",
                    "freeDelivery": True,
                    "orderEntries": [{"quantity": 1, "totalPrice": {"value": "367.5"}, "product": {"code": "W"}}],
                }
            ]
        },
        "cartSubTotal": {"value": "367.50"},
        "hktvDeliveryFee": {"value": {"value": "0"}},
        "otherFeeRemarks": [{"code": "fuelSurcharge", "fee": {"value": "1.1"}}],
        "cartData": {"totalPriceWithTax": {"value": "368.6"}},
    }
    assert cart.view(data)["totals"]["total"]["display"] == "HK$368.60" and not caplog.records
    data["cartData"]["totalPriceWithTax"] = {"value": "318.6"}  # e.g. a voucher this view does not model
    assert cart.view(data)["totals"]["total"]["display"] == "HK$318.60"
    assert "differs from its parts" in caplog.text
