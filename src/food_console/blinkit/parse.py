"""Pure text parsers for Blinkit pages (no browser needed, unit-tested).

The order-history parsers follow the approach of hereisSwapnil/blinkit-mcp
(src/order/services/history.py, MIT licence, Copyright (c) 2025 Swapnil Singh).
"""

from __future__ import annotations

import re
from typing import Any

_PRICE = re.compile(r"^₹\s*([\d,]+(?:\.\d+)?)$")
_ETA = re.compile(r"^(\d+)\s*MINS?$", re.I)
_OFF = re.compile(r"^(\d+)%\s*(OFF)?$", re.I)


def inr(text: str) -> float | None:
    """'₹1,234.50' -> 1234.5; None if there is no rupee amount."""
    m = re.search(r"₹\s*([\d,]+(?:\.\d+)?)", text or "")
    return float(m.group(1).replace(",", "")) if m else None


def parse_card_text(text: str) -> dict[str, Any]:
    """Parse a search-result card's visible text.

    Typical lines: ['17%', 'OFF', '19 MINS', 'Milky Mist Pro+ ...', '250 ml', '₹82', '₹99', 'ADD'].
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    out: dict[str, Any] = {"name": "", "variant": "", "price_inr": None, "mrp_inr": None,
                           "discount_pct": None, "eta_min": None, "in_stock": True}
    rest: list[str] = []
    prices: list[float] = []
    for ln in lines:
        if ln.upper() == "ADD":
            continue
        if ln.upper() == "OFF":
            continue
        if re.match(r"^out of stock$", ln, re.I):
            out["in_stock"] = False
            continue
        if (m := _OFF.match(ln)) and out["discount_pct"] is None:
            out["discount_pct"] = int(m.group(1))
            continue
        if (m := _ETA.match(ln)) and out["eta_min"] is None:
            out["eta_min"] = int(m.group(1))
            continue
        if (m := _PRICE.match(ln)):
            prices.append(float(m.group(1).replace(",", "")))
            continue
        rest.append(ln)
    if rest:
        out["name"] = rest[0]
    if len(rest) > 1:
        out["variant"] = rest[1]
    if prices:
        out["price_inr"] = prices[0]
        if len(prices) > 1 and prices[1] > prices[0]:
            out["mrp_inr"] = prices[1]
    return out


# ---- order history ----

_STATUS = re.compile(r"Arrived|Delivered|Cancelled|Refunded|Failed", re.I)
_TOTAL = re.compile(r"₹[\d,]+(?:\.\d+)?")
_DATE = re.compile(r"\d{1,2} [A-Za-z]{3},? \d{1,2}:\d{2}\s*[ap]m", re.I)


def parse_history_summary(text: str) -> dict[str, str]:
    t = " ".join((text or "").split())
    total_m, date_m = _TOTAL.search(t), _DATE.search(t)
    status = (t[: total_m.start()] if total_m else t).strip()
    return {"status": status, "total": total_m.group(0) if total_m else "", "date": date_m.group(0) if date_m else ""}


def split_qty(line: str) -> tuple[str, int]:
    """'53 g x 1' -> ('53 g', 1); '2 x 750 ml x 1' -> ('2 x 750 ml', 1)."""
    parts = line.rsplit(" x ", 1)
    if len(parts) == 2 and parts[1].strip().isdigit():
        return parts[0].strip(), int(parts[1].strip())
    return line.strip(), 1


def parse_history_detail(text: str) -> dict[str, Any]:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    start = next((i + 1 for i, ln in enumerate(lines) if re.search(r"items? in this order", ln, re.I)), None)
    if start is None:
        return {"items": [], "bill_total": ""}
    items, buf = [], []
    for ln in lines[start:]:
        if re.match(r"bill details", ln, re.I):
            break
        if ln.startswith(("₹", "-₹")):
            if len(buf) >= 2:
                variant, qty = split_qty(buf[-1])
                items.append({"name": " ".join(buf[:-1]), "variant": variant, "quantity": qty, "price": ln})
            buf = []
        else:
            buf.append(ln)
    bill_total = ""
    for i, ln in enumerate(lines):
        if re.fullmatch(r"bill total", ln, re.I):
            bill_total = next((n for n in lines[i + 1:] if n.startswith("₹")), "")
            break
    return {"items": items, "bill_total": bill_total}


def valid_product_id(value: str) -> bool:
    """Blinkit product ids are numeric; anything else is refused (it goes into a CSS selector)."""
    return bool(re.fullmatch(r"\d{1,15}", str(value or "")))
