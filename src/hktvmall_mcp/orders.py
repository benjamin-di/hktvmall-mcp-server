"""Order history for re-ordering: read-only, and only what a re-order needs.

An order's page carries the address, payment and vouchers too; only its item lines are read (site.py), and
only product, quantity and the line total reach a tool result. ``entryPrice`` is the line total paid (the lines sum
to the order total), ``FREE`` marks a gift line.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from .cart import _amount, money
from .fsio import atomic_write_json

log = logging.getLogger(__name__)
FREE = "FREE"
ITEM_KEYS = frozenset({"id", "title", "quantity", "line_total", "free_gift", "status"})


class ItemCache:
    """Completed orders' items, kept as ``orders.json`` beside the account's state: a completed order never changes,
    so its ~2 MB page (1.5-2 s) is read once rather than after every restart. It holds only what a tool result shows
    (``item``'s keys) and goes with the account's directory when the account is disconnected."""

    def __init__(self, path: Path, limit: int = 1000) -> None:
        self.path, self.limit = path, limit
        self._lock = threading.Lock()
        self._orders = self._load()

    def _load(self) -> dict[str, list[dict[str, Any]]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log.warning("orders.json unreadable (%s); starting empty", type(exc).__name__)
            return {}
        orders = data.get("orders") if isinstance(data, dict) else None
        if not isinstance(orders, dict):
            return {}
        return {
            str(number): rows
            for number, rows in orders.items()
            if isinstance(rows, list) and all(isinstance(r, dict) and set(r) <= ITEM_KEYS for r in rows)
        }

    def get(self, number: str) -> list[dict[str, Any]] | None:
        with self._lock:
            return self._orders.get(number)

    def put(self, number: str, rows: list[dict[str, Any]]) -> None:
        with self._lock:
            self._orders[number] = rows
            while len(self._orders) > self.limit:
                self._orders.pop(next(iter(self._orders)))  # the oldest read first
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_json(self.path, {"orders": self._orders})
            except OSError as exc:  # still cached in memory
                log.error("could not persist orders.json (%s)", type(exc).__name__)


def item(entry: dict[str, Any]) -> dict[str, Any]:
    product = entry.get("product") or {}
    paid = entry.get("entryPrice")
    gift = isinstance(paid, str) and paid.strip().upper() == FREE
    row: dict[str, Any] = {
        "id": product.get("code"),
        "title": entry.get("productName") or product.get("name"),
        "quantity": entry.get("quantity"),
        "line_total": money(0 if gift else _amount(paid)),
    }
    if gift:
        row["free_gift"] = True  # came with a promotion; adding it again is a paid item or not possible
    status = str(entry.get("statusCode") or "")
    if status and status != "ORDER_COMPLETE":
        row["status"] = status.lower()
    return row


def delivery(batch: dict[str, Any]) -> dict[str, Any]:
    """One delivery batch of an order (summaryList): when, and whether it arrived."""
    if batch.get("isCancelled"):
        status = "cancelled"
    elif batch.get("isDelivered"):
        status = "delivered"
    elif batch.get("isRescheduled"):
        status = "rescheduled"
    else:
        status = "scheduled"
    row = {"date": batch.get("deliveryDate") or batch.get("deliveryDateText"), "status": status}
    if batch.get("isPickup"):
        row["pickup"] = True
    if batch.get("deliveredByMsg"):
        row["by"] = batch["deliveredByMsg"]
    return row


def view(summary: dict[str, Any], items: list[dict[str, Any]] | None) -> dict[str, Any]:
    """One order; ``items`` None for a summary (the order list alone, without reading the order's page)."""
    total = summary.get("total") or {}
    row = {
        "order": summary.get("code"),
        "placed": summary.get("placed"),
        "status": summary.get("statusDisplay"),
        "total": money(_amount(total)),
        "deliveries": [delivery(b) for b in summary.get("summaryList") or [] if isinstance(b, dict)],
    }
    if items is not None:
        row["items"] = items
    return row
