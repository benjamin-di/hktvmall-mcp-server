"""Unit prices from HKTVmall's pack size (``packingSpecEn``), only when the size is unambiguous.

On a sample of a few hundred supermarket hits it read about four in five sizes and refused the rest
(tests/fixtures/pack_sizes.json pins every branch). The pack size, not the title, is the source: titles drop
multipliers ("Full Case 1L" is ``12x1L``). Ranges, mixed packs ("530ml+530g"), alternatives ("1L / 1000ml"), two
measures and free text are refused, because a wrong unit price misleads more than a missing one.
"""

from __future__ import annotations

import re
from typing import Any

MEASURES = {
    "ml": ("ml", 1),
    "milliliters": ("ml", 1),
    "l": ("ml", 1000),
    "ltr": ("ml", 1000),
    "litre": ("ml", 1000),
    "liter": ("ml", 1000),
    "g": ("g", 1),
    "gm": ("g", 1),
    "kg": ("g", 1000),
}
# Counted things that multiply a measure ("90g x 5 pcs"); only rolls and pieces become a unit of their own.
COUNTS = {"pcs", "pc", "pieces", "piece", "eggs", "rolls", "roll", "packs", "pack", "pk", "bags", "bottles", "cans"}
COUNT_UNIT = {"rolls": "roll", "roll": "roll", "pcs": "piece", "pc": "piece", "pieces": "piece", "piece": "piece"}
COUNT_UNIT["eggs"] = "piece"
AMBIGUOUS = re.compile(r"[+~/]|\d\s*-\s*\d")
PART = re.compile(r"(\d+(?:\.\d+)?)\s*([a-z]+)?\.?", re.I)
PAREN = re.compile(r"\((\d+(?:\.\d+)?)\s*(ml|l|g|kg)\)", re.I)
SUFFIX = re.compile(r"\s*\b(?:family\s+)?pack$", re.I)


def parse(spec: str | None) -> tuple[str, float] | None:
    """('ml'|'g', total) or ('roll'|'piece', count), or None when the size is not certain."""
    if not spec or AMBIGUOUS.search(spec):
        return None
    s = SUFFIX.sub("", spec.strip())
    paren = PAREN.search(s)
    if paren:  # "6-slice (230g)": the weight in brackets is the size
        s = paren.group(1) + paren.group(2)
    parts = [p.strip() for p in re.split(r"\s*[x×*]\s*", s, flags=re.I) if p.strip()]
    measure: tuple[str, float] | None = None
    multiplier, counted = 1.0, None
    for part in parts:
        m = PART.fullmatch(part)
        if not m:
            return None
        n, unit = float(m.group(1)), (m.group(2) or "").lower()
        if n <= 0:
            return None
        if unit in MEASURES:
            if measure:
                return None
            base, factor = MEASURES[unit]
            measure = (base, n * factor)
        elif unit in COUNTS or unit == "":
            multiplier *= n
            counted = COUNT_UNIT.get(unit, counted)
        else:
            return None
    if measure:
        return measure[0], measure[1] * multiplier
    if counted and len(parts) <= 2:
        return counted, multiplier
    return None


def unit_price(cents: int | None, spec: str | None) -> dict[str, Any] | None:
    """{cents, per} per 100ml, 100g, roll or piece; None when the price or the size is not certain."""
    size = parse(spec)
    if not size or not cents:
        return None
    base, qty = size
    if base in ("ml", "g"):
        return {"cents": round(cents * 100 / qty), "per": f"100{base}"}
    return {"cents": round(cents / qty), "per": base}
