"""The basket view and the update flow.

The rule this module exists for: the result of a change is what the basket shows afterwards, never what
HKTVmall's write call said (observed: an add reported success for an item it dropped; an update to 500
reported SUCCESS and stored 51; +50 reported quantityAdded 50 and changed nothing).
"""

from __future__ import annotations

import html
import logging
import re
import time
from datetime import datetime
from decimal import Decimal
from typing import Any

from . import links
from .errors import HktvError, invalid
from .site import BASE, cents
from .state import HKT

log = logging.getLogger(__name__)

LINE_CAP = 15  # per line; above it the person must agree first
VALUE_CAP = 150_000  # HKD cents added in one call
CHECKOUT_URL = BASE + "/hktv/en/cart"


def money(c: int | None) -> dict[str, Any] | None:
    if c is None:
        return None
    return {"cents": c, "display": f"HK${Decimal(c) / 100:,.2f}"}


def _amount(value: Any) -> int | None:
    """'$ 288.00' or {'value': '288.0'} -> 28800."""
    if isinstance(value, dict):
        return cents(value.get("value"))
    if isinstance(value, str):
        digits = value.replace("$", "").replace(",", "").strip()
        return cents(digits) if digits else None
    return None


# Stock codes HKTVmall uses for "cannot be bought now" (observed: stockNotifiable; outOfStock in page data).
# Anything else (inStock, lowStock, an unknown new code) is sent, and the read-back has the last word.
UNAVAILABLE = frozenset({"outOfStock", "stockNotifiable"})


def stock_code(product: dict[str, Any] | None) -> str | None:
    return ((product or {}).get("stock") or {}).get("stockLevelStatus", {}).get("code")


def in_stock(product: dict[str, Any] | None) -> bool:
    return bool(product) and stock_code(product) not in UNAVAILABLE


def store_closed(product: dict[str, Any] | None) -> bool:
    """The seller's shop is temporarily closed (a holiday): HKTVmall answers an add with success and drops
    it (observed 2026-10-19), while the search index still shows the item in stock."""
    return bool(product) and product.get("storeClosedStatus") == 1


def reopens_at(product: dict[str, Any], now: float | None = None) -> str | None:
    """End of the closed-store window that is current or next, in Hong Kong time, from HKTVmall's schedule."""
    now_ms = (time.time() if now is None else now) * 1000
    ends = [
        w.get("endTime")
        for w in product.get("deliveryLabelScheduleCode") or []
        if isinstance(w, dict) and w.get("deliveryMode") == "closed-store"
    ]
    ends = sorted(e for e in ends if isinstance(e, int | float) and e > now_ms)
    return datetime.fromtimestamp(ends[0] / 1000, HKT).isoformat(timespec="minutes") if ends else None


def view(data: dict[str, Any]) -> dict[str, Any]:
    """HKTVmall's checkout payload -> the agent's basket."""
    lines, groups = [], []
    for g in data.get("cartDataGroupByDeliveryDate", {}).get("deliveryDateOrderEntries", []):
        gid = f"batch-{g.get('batchNumber')}"
        subtotal = 0
        for e in g.get("orderEntries", []):
            p = e.get("product", {})
            unit = _amount(e.get("discountedUnitPrice"))
            if unit is None:  # only a missing price falls back; 0 is a free line, not an absent one
                unit = _amount(e.get("basePrice"))
            base = _amount(e.get("basePrice"))
            total = _amount(e.get("totalPrice")) or 0
            subtotal += total
            lines.append(
                {
                    "id": p.get("code"),
                    "title": p.get("name"),
                    "url": links.product_url(p.get("code"), p.get("url")),
                    "quantity": e.get("quantity"),
                    "unit_price": money(unit),
                    "list_price": money(base) if base and base != unit else None,
                    "total": money(total),
                    "available_quantity": (p.get("stock") or {}).get("stockLevel"),
                    "delivery_group": gid,
                }
            )
        groups.append(
            {
                "id": gid,
                "title": g.get("groupTitle"),
                "mode": g.get("deliveryMode"),
                "subtotal": money(subtotal),
                "free_delivery": bool(g.get("freeDelivery")),
            }
        )
    threshold = _amount(data.get("deliveryThreshold"))
    hktv_fee = _amount((data.get("hktvDeliveryFee") or {}).get("value"))
    for g in groups:
        if g["mode"] == "hktv-standard-delivery" and threshold and not g["free_delivery"]:
            g["amount_to_free_delivery"] = money(max(0, threshold - g["subtotal"]["cents"]))
            g["delivery_fee"] = money(hktv_fee)
    merchant_fees = sum(_amount(f.get("fee")) or 0 for f in data.get("merchantDeliveryFeeList") or [])
    subtotal = _amount(data.get("cartSubTotal")) or 0
    # hktvDeliveryFee is the "HKTV Delivery Fee" line of HKTVmall's order summary and follows the threshold;
    # deliveryCost only fills the summary's "HK$40 below HK$88" tip, so it is a fallback for an older payload.
    delivery = hktv_fee if hktv_fee is not None else _amount(data.get("deliveryCost")) or 0
    # Fees HKTVmall lists beside delivery (fuel surcharge, plastic bag levy), charged at checkout.
    other_fees = sum(_amount(f.get("fee")) or 0 for f in data.get("otherFeeRemarks") or [] if isinstance(f, dict))
    computed = subtotal + delivery + merchant_fees + other_fees
    # The order summary's "Total payment" line; it includes anything not modelled here (vouchers, new fees).
    total = _amount((data.get("cartData") or {}).get("totalPriceWithTax"))
    if total is None:
        total = computed
    elif total != computed and lines:
        log.warning("basket total %d differs from its parts %d; reporting HKTVmall's total", total, computed)
    if not lines:
        subtotal = delivery = merchant_fees = other_fees = total = 0
    notes = warning_notes(list(data.get("errorMessages") or []))
    return {
        **({"notes": notes} if notes else {}),
        "line_items": lines,
        "delivery_groups": groups,
        "totals": {
            "subtotal": money(subtotal),
            "delivery_fee": money(delivery + merchant_fees),
            "other_fees": money(other_fees),
            "total": money(total),
            "free_delivery_threshold": money(threshold),
        },
        "checkout_url": CHECKOUT_URL,
    }


WARNING_CHARS = 300


def warning_notes(messages: list[Any]) -> list[dict[str, Any]]:
    """HKTVmall's checkout warnings (plain strings, sometimes with markup) as notes, without repeats."""
    seen, out = set(), []
    for m in messages:
        text = html.unescape(re.sub(r"<[^>]+>", " ", m)) if isinstance(m, str) else ""
        text = re.sub(r"\s+", " ", text).strip()[:WARNING_CHARS]
        if text and text not in seen:
            seen.add(text)
            out.append({"code": "hktvmall_warning", "item_id": None, "text": text})
    return out


def quantities(basket: dict[str, Any]) -> dict[str, int]:
    return {line["id"]: line["quantity"] for line in basket["line_items"]}


def precheck(
    changes: list[dict[str, Any]], have: dict[str, int], prices: dict[str, int], confirmed: bool
) -> dict[str, Any] | None:
    """Caps before anything is sent. Returns a refusal or None."""
    ids = [c["id"] for c in changes]
    if len(set(ids)) != len(ids):
        raise invalid("each id may appear once in changes")
    if confirmed:
        return None
    # Only increases need the person's agreement: lowering a line the person set high never does.
    over = [c["id"] for c in changes if c["quantity"] > LINE_CAP and c["quantity"] > have.get(c["id"], 0)]
    added = sum(max(0, c["quantity"] - have.get(c["id"], 0)) * prices.get(c["id"], 0) for c in changes)
    if not over and added <= VALUE_CAP:
        return None
    notes = [{"code": "over_cap", "item_id": i, "text": f"more than {LINE_CAP} of one item"} for i in over]
    if added > VALUE_CAP:
        notes.append({"code": "over_cap", "item_id": None, "text": f"adds {money(added)['display']} in one call"})
    return {
        "status": "requires_buyer_review",
        "changes": [],
        "notes": notes,
        "message": "Nothing was changed. Ask the person; if they agree, call again with confirmed=true.",
    }


def reconcile(
    changes: list[dict[str, Any]],
    before: dict[str, int],
    after: dict[str, Any],
    products: dict[str, dict[str, Any]],
    skipped: dict[str, str],
    sent: set[str],
) -> list[dict[str, Any]]:
    """Each change's outcome from what was sent and what the basket shows afterwards (``after`` is the view).

    A change that was never sent (the call stopped first) is ``not_sent``, never blamed on HKTVmall; a change
    HKTVmall ignored is ``rejected``; one it limited is ``adjusted``, with ``stock_limit`` decided by the stock
    level the basket itself reports (search results carry no stock level).
    """
    lines = {line["id"]: line for line in after["line_items"]}
    out = []
    for c in changes:
        sku, want, have = c["id"], c["quantity"], before.get(c["id"], 0)
        got = (lines.get(sku) or {}).get("quantity") or 0
        row: dict[str, Any] = {"id": sku, "requested": want, "in_cart": got}
        if sku in skipped:
            row.update(outcome="rejected", reason=skipped[sku])
            if skipped[sku] == "store_closed":
                row["reopens_at"] = reopens_at(products[sku])
        elif got == want:
            row["outcome"] = "applied"
        elif sku not in sent and want != have:
            row.update(outcome="rejected", reason="not_sent")
        elif want == 0:
            row.update(outcome="rejected", reason="not_removed")
        elif got == have:  # nothing changed
            if want < have:
                row.update(outcome="rejected", reason="not_lowered")
            else:
                p = products.get(sku)
                reason = "not_found" if p is None and have == 0 else "out_of_stock" if p and not in_stock(p) else ""
                row.update(outcome="rejected", reason=reason or "not_accepted")
        elif got == 0:  # the line is gone: HKTVmall dropped it, which is not a limit
            row.update(outcome="rejected", reason="dropped")
        else:
            stock = (lines.get(sku) or {}).get("available_quantity")
            limited = isinstance(stock, int) and got >= stock and want > stock
            row.update(outcome="adjusted", reason="stock_limit" if limited else "site_limit")
        out.append(row)
    return out


def update(site, changes: list[dict[str, Any]], confirmed: bool) -> dict[str, Any]:
    """caps -> skip known-bad -> apply -> read back (repair a zero-quantity line once) -> reconcile."""
    with site.account() as acct:
        before_basket = view(acct.basket())
        before = quantities(before_basket)
        products = site.lookup([c["id"] for c in changes])
        increases = [c["id"] for c in changes if c["quantity"] > before.get(c["id"], 0)]
        if increases and not products and not site.search_works():
            # Search answering nothing at all is an outage, not "none of these are sold": change nothing.
            raise HktvError("upstream_error", log_detail="search empty during update")
        prices = {i: cents(p.get("sellingPrice")) for i, p in products.items()}
        for line in before_basket["line_items"]:  # the basket prices a line it holds when search cannot
            if prices.get(line["id"]) is None and line.get("unit_price"):
                prices[line["id"]] = line["unit_price"]["cents"]
        unpriced = {i for i in increases if i in products and prices.get(i) is None}
        prices = {i: c for i, c in prices.items() if c is not None}
        refusal = precheck(changes, before, prices, confirmed)
        if refusal:
            return {**refusal, "basket": before_basket}
        skipped: dict[str, str] = {}
        adds, sets, removes = [], [], []
        for c in changes:
            sku, want, have = c["id"], c["quantity"], before.get(c["id"], 0)
            if want == have:
                continue
            p = products.get(sku)
            if want > have and p is None and have == 0:  # a line already in the basket needs no product data
                skipped[sku] = "not_found"
            elif sku in unpriced:  # an unknown price would pass the HK$1,500 check as free
                skipped[sku] = "no_price"
            elif want > have and p is not None and store_closed(p):
                skipped[sku] = "store_closed"
            elif want > have and p is not None and not in_stock(p):
                skipped[sku] = "out_of_stock"
            elif want == 0:
                removes.append(sku)
            elif have == 0:
                adds.append((sku, want))
            else:
                sets.append((sku, want))
        if adds:
            acct.verify()  # CSRF for the legacy add, and proof the sign-in is alive (raises before any write)
        stopped: str | None = None
        replies: dict[str, Any] = {}
        sent: set[str] = set()  # marked before each call: a call that fails half-way may still have applied
        try:
            for sku, want in adds:
                sent.add(sku)
                replies[sku] = acct.add(sku, want)
            if adds:
                acct.remint()  # each legacy call cleared access_token
            for sku, want in sets:
                sent.add(sku)
                acct.set_quantity(sku, want)
            for sku in removes:
                sent.add(sku)
                acct.remove(sku)
        except Exception as err:  # noqa: BLE001 - something may already have changed: read back, report
            stopped = err.code if isinstance(err, HktvError) else "internal_error"
        try:
            after_raw = acct.basket()
        except HktvError as err:
            if err.code != "basket_unreadable":
                raise HktvError("outcome_unknown", log_detail=err.code) from None
            # A zero-quantity line breaks every read and removing it repairs the basket. Only lines this
            # call removes can be one; lines being set to a positive quantity are never touched here.
            try:
                for sku in removes:
                    acct.remove(sku)
                after_raw = acct.basket()
            except HktvError:
                raise HktvError("outcome_unknown", log_detail="read-back failed after repair") from None
        basket = view(after_raw)
        rows = reconcile(changes, before, basket, products, skipped, sent)
        for r in rows:  # the evidence for the next unexplained drop: outcome, reason and HKTVmall's add reply
            if r["outcome"] != "applied":
                log.info(
                    "change %s: %s %s (requested %s, in basket %s, add reply %s)",
                    r["id"],
                    r["outcome"],
                    r.get("reason", "-"),
                    r["requested"],
                    r["in_cart"],
                    replies.get(r["id"], "-"),
                )
        status = "ok" if all(r["outcome"] == "applied" for r in rows) else "partial"
        out: dict[str, Any] = {"status": status, "changes": rows, "basket": basket}
        if stopped is not None:
            out["notes"] = [
                {
                    "code": "stopped",
                    "item_id": None,
                    "text": f"HKTVmall failed part-way ({stopped}); the outcomes show what took effect",
                }
            ]
        return out
