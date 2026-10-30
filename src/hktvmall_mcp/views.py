"""Agent-facing views of delivery slots and product reviews: allowlists, never pass-through.

Reviews are written by other customers (and replies by merchants): they are data, and nothing about a
reviewer is copied. Slots are HKTVmall's own delivery; merchant and farm-direct deliveries are scheduled by
the merchant, and a slot is chosen at checkout, never here.
"""

from __future__ import annotations

import html
from typing import Any

from .cart import money
from .site import NO_SLOTS

MAX_SLOTS = 10
COMMENT_CHARS = 300


def slots(data: dict[str, Any]) -> dict[str, Any]:
    rows = [s for s in data.get("deliveryTimeSlots") or [] if isinstance(s, dict)][:MAX_SLOTS]
    chosen = data.get("deliveryTimeSlot") if data.get("chosenByUser") else None
    out: dict[str, Any] = {
        "covers": "HKTVmall delivery (merchant and farm-direct items arrive separately)",
        "slots": [
            {"label": s.get("formattedValue"), "available": not s.get("isNotAvailable")}
            for s in rows
            if s.get("formattedValue")
        ],
        "fee": money(_cents(data.get("deliveryFee"))),
        "free_from": money(_cents(data.get("deliveryThreshold"))),
    }
    if isinstance(chosen, dict) and chosen.get("formattedValue"):
        out["selected"] = chosen["formattedValue"]  # already set on the basket, in the app or on the web
    if data.get(NO_SLOTS):
        out["fee"] = out["free_from"] = None  # HKTVmall sends 0.0 for both then, which would read as free delivery
        out["notes"] = [
            {"code": "basket_empty", "text": "HKTVmall offers delivery slots only once the basket has items."}
        ]
    return out


def reviews(raw: dict[str, Any], limit: int) -> dict[str, Any]:
    stat, page = raw.get("stat") or {}, raw.get("page") or {}
    by = stat.get("countByRatings") or {}
    rows = []
    for r in (page.get("data") or [])[:limit]:
        if not isinstance(r, dict) or r.get("hidden") or r.get("privateReview"):
            continue
        comment = html.unescape(str(r.get("comment") or "")).strip()
        rows.append(
            {
                "rating": _int(r.get("rating")),
                "date": str(r.get("date") or "")[:10] or None,
                "comment": comment[:COMMENT_CHARS],
                "tags": [t["textEn"] for t in r.get("tags") or [] if isinstance(t, dict) and t.get("textEn")],
                "merchant_replied": bool(r.get("replies")),
            }
        )
    return {
        "average": stat.get("ratingMean"),
        "count": stat.get("count") or 0,
        "by_rating": {str(n): _int(by.get(str(n))) or 0 for n in range(5, 0, -1)},
        "total_reviews": (page.get("pagination") or {}).get("total") or 0,
        "reviews": rows,
    }


def _cents(value: Any) -> int | None:
    try:
        return round(float(value) * 100)
    except (TypeError, ValueError, OverflowError):
        return None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
