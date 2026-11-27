"""Joint Zomato + Swiggy order history: extraction via Claude, metrics in plain Python."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .agent import OrderAgent
from .mcp_bridge import SERVER_BRANDS, BridgedTool
from .store import Store

# Read-only tools that can help list orders (plus addresses, which Swiggy Food needs).
_HISTORY_TOOL = re.compile(r"order|history|booking|address", re.I)


# District exposes everything through two generic read-only tools: `search` finds an
# operation (e.g. consumer.booking_history) and `execute` runs it.
_DISPATCH_TOOLS = {"district": {"search", "execute"}}


def history_tool_filter(tool: BridgedTool) -> bool:
    if not tool.read_only:
        return False
    return bool(_HISTORY_TOOL.search(tool.mcp_name)) or tool.mcp_name in _DISPATCH_TOOLS.get(tool.server, set())


CATEGORIES = [
    "meal", "fast_food", "fried_snack", "dessert", "sugary_drink", "healthy_drink",
    "fruit_veg", "protein_dairy", "staple_grocery", "packaged_snack", "household", "other",
]

ORDER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "orders": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "provider": {"type": "string", "enum": ["zomato", "swiggy", "district", "zepto", "blinkit"]},
                    "service": {
                        "type": "string",
                        "enum": ["food", "instamart", "dineout", "events", "grocery"],
                        "description": (
                            "food = restaurant/cafe delivery; instamart = Swiggy Instamart; grocery = Zepto or "
                            "Blinkit groceries and essentials; dineout = table bookings/dining bills; events = movies, events"
                        ),
                    },
                    "order_id": {"type": "string"},
                    "placed_at": {
                        "type": "string",
                        "description": "When the order was placed: ISO 8601 exactly as returned (keep the timezone), or empty",
                    },
                    "delivered_at": {
                        "type": "string",
                        "description": "When it was delivered/completed, ISO 8601 as returned, or empty if unknown",
                    },
                    "merchant": {"type": "string", "description": "Restaurant or store name"},
                    "status": {"type": "string"},
                    "total_inr": {"type": "number", "description": "Amount paid in INR; 0 if unknown"},
                    "discount_inr": {"type": "number", "description": "Total discount/savings in INR; 0 if unknown"},
                    "delivery_fee_inr": {"type": "number", "description": "Delivery + platform fees in INR; 0 if unknown"},
                    "payment_method": {"type": "string", "description": "e.g. UPI, CARD, COD; empty if unknown"},
                    "address_label": {"type": "string", "description": "Short delivery address label, e.g. Home; empty if unknown"},
                    "item_count": {"type": "integer"},
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "quantity": {"type": "integer"},
                                "category": {"type": "string", "enum": CATEGORIES},
                                "health": {
                                    "type": "string",
                                    "enum": ["healthy", "balanced", "indulgent", "non_food"],
                                    "description": "Your nutritional judgement of the item",
                                },
                                "veg": {"type": "string", "enum": ["veg", "non_veg", "unknown"]},
                            },
                            "required": ["name", "quantity", "category", "health", "veg"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": [
                    "provider", "service", "order_id", "placed_at", "delivered_at", "merchant", "status",
                    "total_inr", "discount_inr", "delivery_fee_inr", "payment_method", "address_label",
                    "item_count", "items",
                ],
                "additionalProperties": False,
            },
        },
        "notes": {"type": "string", "description": "Gaps or problems, e.g. a service that returned no history"},
    },
    "required": ["orders", "notes"],
    "additionalProperties": False,
}

EXTRACT_SYSTEM = """You collect the user's order history from their food-delivery accounts \
using read-only MCP tools, then return it as JSON matching the given schema.

- Call every order-history tool that is available (Zomato, Swiggy Food, Swiggy Instamart, Zepto, Blinkit, \
District; Swiggy Dineout has no history list, so skip it unless a tool lists bookings). District \
bookings use provider "district" with service "dineout" for restaurant dining/table bookings and \
"events" for movies, events and activities (tag their items non_food unless they are food). \
On District, call search for "my bookings" and for "bill payment history", then run the \
operations it returns (consumer.booking_history and the bill payment history one) with execute, \
paging with next_postback_params. Restaurant bills paid through District are service "dineout". \
Zepto orders use provider "zepto" with service "grocery", or "food" for Zepto Cafe orders. \
Zepto's list_order_history has no dates: for each order call get_order_detail with the order's \
`id` (the UUID, not the short `code`) to get placedTime, deliveryTime, totalSaved and the items; \
use `code` as order_id. \
Blinkit orders use provider "blinkit", service "grocery"; Blinkit dates have no year (e.g. \
"12 Nov, 7:45 pm"): use the current year unless that would be in the future, then the year before, \
and write them in ISO 8601 with +05:30.
- Go back as far as the tools allow, up to {limit} orders per service. Set every page-size \
parameter (orderCount, count, limit, ...) to the maximum its description states rather than \
leaving the default, try each order-type filter the tool documents (e.g. Instamart's orderType), \
and follow pagination until there are no more pages or you reach the limit - whatever style \
the tool uses: a cursor (e.g. nextCursor), or a has_more flag with postback/continuation params \
(e.g. Zomato: call again with request_type "load_more", has_more true and the postback_params \
string from the previous result, copied exactly). If a tool \
needs an address id, call the addresses tool first and use the first address.
- Copy values exactly from tool results: ids, timestamps (placed and delivered/updated time of \
delivered orders), amounts, discounts, fees, payment method, address label, items. Never invent \
orders, amounts or dates. Use 0 / empty string when a value is missing.
- For every item, add your own judgement: `category`, `health` (healthy = whole foods, fruit, \
vegetables, salads, lean protein, plain dairy, grilled/steamed dishes; balanced = regular home-style \
meals, rice/roti/dal/curries, staples; indulgent = deep-fried, pizza/burgers, desserts, sweets, \
sugary drinks, chips and packaged snacks; non_food = household or personal care) and `veg`.
- If a service fails or returns nothing, leave it out and say so in `notes`."""


def format_schema() -> dict[str, Any]:
    return {"type": "json_schema", "schema": ORDER_SCHEMA}


async def extract_orders(agent: OrderAgent, limit: int = 50) -> dict[str, Any]:
    """Run the extraction agent and return {"orders": [...], "notes": str, "fetched_at": iso}."""
    agent.system = EXTRACT_SYSTEM.format(limit=limit)
    text = await agent.send(
        "Collect my order history now.", output_format=format_schema(), max_tokens=128000
    )
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"extraction did not return JSON: {text[:300]}") from exc
    data["fetched_at"] = datetime.now(timezone.utc).isoformat()
    return data


def _key(o: dict[str, Any]) -> tuple[str, str, str]:
    return (o.get("provider", ""), o.get("service", ""), str(o.get("order_id", "")))


def merge_orders(old: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any]:
    """Keep orders from earlier refreshes (providers only return recent ones); newer copies win."""
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    for o in (old or {}).get("orders", []):
        merged[_key(o)] = o
    for o in new.get("orders", []):
        if not o.get("order_id"):  # no id: can't dedupe, keep as-is
            merged[(o.get("provider", ""), o.get("service", ""), f"_{len(merged)}")] = o
        else:
            merged[_key(o)] = o
    added = len(merged) - len((old or {}).get("orders", []))
    return {**new, "orders": list(merged.values()), "added": max(0, added)}


def save_orders(data: dict[str, Any], path: Path | None = None) -> None:
    """Persist the (already merged) order history in the local SQLite store."""
    s = Store(path)
    try:
        s.save_orders(data)
    finally:
        s.close()


def load_orders(path: Path | None = None) -> dict[str, Any] | None:
    s = Store(path)
    try:
        return s.load_orders()
    finally:
        s.close()


_CANCELLED = re.compile(r"cancel|fail|refund|reject", re.I)
_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone()  # local time of this machine


def filter_orders(orders: list[dict[str, Any]], start: date | None, end: date | None) -> list[dict[str, Any]]:
    """Orders placed between start and end (local dates, inclusive). Undated orders only without a range."""
    if start is None and end is None:
        return list(orders)
    out = []
    for o in orders:
        dt = parse_time(o.get("placed_at", ""))
        if dt is None:
            continue
        d = dt.date()
        if (start is None or d >= start) and (end is None or d <= end):
            out.append(o)
    return out


def previous_period(start: date, end: date) -> tuple[date, date]:
    span = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    return prev_end - timedelta(days=span - 1), prev_end


def _bucket() -> dict[str, float]:
    return {"orders": 0, "spend": 0.0}


def _finish(b: dict[str, float]) -> dict[str, float]:
    return {**b, "spend": round(b["spend"], 2), "aov": round(b["spend"] / b["orders"], 2) if b["orders"] else 0.0}


def _timeline(done: list[tuple[datetime, dict[str, Any]]], start: date | None, end: date | None) -> dict[str, Any]:
    """Spend/orders per day, week or month across the whole range, empty buckets included."""
    dates = [dt.date() for dt, _ in done]
    if start is None:
        start = min(dates) if dates else None
    if end is None:
        end = max(dates) if dates else None
    if start is None or end is None or end < start:
        return {"granularity": "day", "buckets": []}
    days = (end - start).days + 1
    if days <= 45:
        gran, key = "day", (lambda d: d)
    elif days <= 200:
        gran, key = "week", (lambda d: d - timedelta(days=d.weekday()))
    else:
        gran, key = "month", (lambda d: d.replace(day=1))

    buckets: dict[date, dict[str, Any]] = {}
    d = key(start)
    while d <= end:
        buckets[d] = {"start": d.isoformat(), "zomato": 0.0, "swiggy": 0.0, "orders": 0}
        if gran == "day":
            d += timedelta(days=1)
        elif gran == "week":
            d += timedelta(days=7)
        else:
            d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    for dt, o in done:
        b = buckets.get(key(dt.date()))
        if b is None:
            continue
        b["orders"] += 1
        p = o.get("provider")
        if p in ("zomato", "swiggy"):
            b[p] = round(b[p] + float(o.get("total_inr") or 0), 2)
    return {"granularity": gran, "buckets": list(buckets.values())}


def _status(value: float, good: float, bad: float, higher_is_better: bool = False) -> str:
    if higher_is_better:
        return "good" if value >= good else "bad" if value <= bad else "watch"
    return "good" if value <= good else "bad" if value >= bad else "watch"


def health_metrics(done: list[dict[str, Any]], dated: list[datetime], weeks: float) -> dict[str, Any] | None:
    """Heuristic eating-habit signals from item tags. Not medical advice."""
    qty: Counter[str] = Counter()
    cats: Counter[str] = Counter()
    veg: Counter[str] = Counter()
    for o in done:
        for it in o.get("items", []):
            q = int(it.get("quantity") or 1)
            h = it.get("health")
            if h in ("healthy", "balanced", "indulgent"):
                qty[h] += q
                cats[it.get("category", "other")] += q
                if it.get("veg") in ("veg", "non_veg"):
                    veg[it["veg"]] += q
    food_items = sum(qty.values())
    if not food_items:
        return None

    indulgent = qty["indulgent"] / food_items
    healthy = qty["healthy"] / food_items
    late = sum(1 for dt in dated if dt.hour >= 22 or dt.hour < 4)
    late_share = late / len(dated) if dated else 0.0
    deliveries = sum(1 for o in done if o.get("service") == "food")
    per_week = deliveries / max(weeks, 1.0)
    sugary, desserts, fruit_veg = cats["sugary_drink"], cats["dessert"], cats["fruit_veg"]
    fried = cats["fried_snack"] + cats["fast_food"]
    veg_share = veg["veg"] / sum(veg.values()) if veg else None

    # Weights chosen so the score agrees with the factor statuses below: e.g. 40% indulgent
    # items (a "watch") alone costs 28 points.
    score = 100.0
    score -= indulgent * 70
    score -= late_share * 30
    score -= min(max(per_week - 3, 0) * 4, 20)
    score += healthy * 15
    score = max(0, min(100, round(score)))
    label = "Great" if score >= 80 else "Good" if score >= 65 else "Fair" if score >= 45 else "Needs attention"

    factors = [
        {"key": "indulgent", "label": "Indulgent items", "value": f"{indulgent:.0%}",
         "status": _status(indulgent, 0.25, 0.5),
         "hint": "Share of food items that are fried, fast food, sweets or sugary drinks."},
        {"key": "healthy", "label": "Healthy items", "value": f"{healthy:.0%}",
         "status": _status(healthy, 0.3, 0.1, higher_is_better=True),
         "hint": "Fruit, vegetables, salads, lean protein and grilled or steamed dishes."},
        {"key": "late_night", "label": "Late-night orders", "value": f"{late_share:.0%}",
         "status": _status(late_share, 0.1, 0.3),
         "hint": "Orders placed between 10 pm and 4 am."},
        {"key": "frequency", "label": "Food deliveries per week", "value": f"{per_week:.1f}",
         "status": _status(per_week, 3, 6),
         "hint": "Restaurant deliveries only; groceries are not counted."},
        {"key": "fried", "label": "Fried and fast food", "value": str(fried),
         "status": _status(fried / food_items, 0.15, 0.35),
         "hint": "Items such as burgers, pizza, fries, pakoras and other deep-fried snacks."},
        {"key": "sugary", "label": "Sugary drinks", "value": str(sugary),
         "status": _status(sugary / food_items, 0.05, 0.15),
         "hint": "Soft drinks, shakes and other sweetened beverages."},
        {"key": "desserts", "label": "Desserts and sweets", "value": str(desserts),
         "status": _status(desserts / food_items, 0.08, 0.2),
         "hint": "Cakes, ice cream, mithai and chocolates."},
        {"key": "fruit_veg", "label": "Fruit and vegetables", "value": str(fruit_veg),
         "status": _status(fruit_veg / food_items, 0.2, 0.05, higher_is_better=True),
         "hint": "Fresh produce, mostly from grocery orders."},
    ]
    if veg_share is not None:
        factors.append({"key": "veg", "label": "Vegetarian items", "value": f"{veg_share:.0%}", "status": "info",
                        "hint": "Share of food items tagged vegetarian."})
    return {
        "score": score,
        "label": label,
        "mix": {k: qty[k] for k in ("healthy", "balanced", "indulgent")},
        "categories": dict(cats.most_common()),
        "factors": factors,
    }


def compute_metrics(
    orders: list[dict[str, Any]], start: date | None = None, end: date | None = None
) -> dict[str, Any]:
    """Aggregate a normalized order list into joint metrics. Cancelled orders count separately.

    `start`/`end` (local dates) only shape the timeline and per-week rates; filter first with
    `filter_orders`.
    """
    done = [o for o in orders if not _CANCELLED.search(o.get("status", ""))]
    cancelled = len(orders) - len(done)

    total = _bucket()
    by_provider: dict[str, dict[str, float]] = defaultdict(_bucket)
    by_service: dict[str, dict[str, float]] = defaultdict(_bucket)
    merchants: dict[tuple[str, str], dict[str, float]] = defaultdict(_bucket)
    items: Counter[str] = Counter()
    payments: Counter[str] = Counter()
    weekday = [0] * 7
    hour = [0] * 24
    heatmap = [[0] * 24 for _ in range(7)]
    late_night = 0
    biggest: dict[str, Any] | None = None
    dates: list[datetime] = []
    dated_done: list[tuple[datetime, dict[str, Any]]] = []
    delivery_minutes: list[float] = []
    discount = fees = 0.0

    for o in done:
        amount = float(o.get("total_inr") or 0)
        provider = o.get("provider", "unknown")
        service = f"{provider}:{o.get('service', 'food')}"
        for b in (total, by_provider[provider], by_service[service]):
            b["orders"] += 1
            b["spend"] += amount
        m = merchants[(o.get("merchant") or "Unknown", provider)]
        m["orders"] += 1
        m["spend"] += amount
        discount += float(o.get("discount_inr") or 0)
        fees += float(o.get("delivery_fee_inr") or 0)
        if o.get("payment_method"):
            payments[o["payment_method"].upper()] += 1
        for it in o.get("items", []):
            items[it.get("name", "?")] += int(it.get("quantity") or 1)
        if biggest is None or amount > biggest["total_inr"]:
            biggest = {"merchant": o.get("merchant") or "Unknown", "provider": provider,
                       "service": o.get("service", "food"), "total_inr": amount, "placed_at": o.get("placed_at", "")}
        dt = parse_time(o.get("placed_at", ""))
        if dt:
            dates.append(dt)
            dated_done.append((dt, o))
            weekday[dt.weekday()] += 1
            hour[dt.hour] += 1
            heatmap[dt.weekday()][dt.hour] += 1
            if dt.hour >= 22 or dt.hour < 4:
                late_night += 1
            delivered = parse_time(o.get("delivered_at", ""))
            if delivered and delivered > dt:
                minutes = (delivered - dt).total_seconds() / 60
                if minutes < 6 * 60:  # ignore obviously wrong pairs
                    delivery_minutes.append(minutes)

    first = min(dates).date() if dates else None
    last = max(dates).date() if dates else None
    span_start, span_end = start or first, end or last
    weeks = ((span_end - span_start).days + 1) / 7 if span_start and span_end else 1.0

    top_merchants = sorted(merchants.items(), key=lambda kv: kv[1]["spend"], reverse=True)[:10]
    by_count = max(merchants.items(), key=lambda kv: (kv[1]["orders"], kv[1]["spend"]), default=None)
    busiest = max(range(7), key=lambda d: weekday[d]) if any(weekday) else None
    return {
        "total": {**_finish(total), "cancelled": cancelled, "all_orders": len(orders)},
        "first_order": min(dates).isoformat() if dates else None,
        "last_order": max(dates).isoformat() if dates else None,
        "favorite": {"merchant": by_count[0][0], "provider": by_count[0][1], "orders": by_count[1]["orders"]}
        if by_count else None,
        "biggest_order": biggest,
        "late_night": late_night,
        "busiest_day": _WEEKDAYS[busiest] if busiest is not None else None,
        "peak_hour": max(range(24), key=lambda h: hour[h]) if any(hour) else None,
        "late_night_share": round(late_night / len(dates), 3) if dates else 0.0,
        "items_total": sum(items.values()),
        "orders_per_week": round(total["orders"] / max(weeks, 1.0), 2),
        "savings": {"discount": round(discount, 2), "fees": round(fees, 2)},
        "delivery": {
            "avg_minutes": round(sum(delivery_minutes) / len(delivery_minutes), 1) if delivery_minutes else None,
            "fastest": round(min(delivery_minutes), 1) if delivery_minutes else None,
            "slowest": round(max(delivery_minutes), 1) if delivery_minutes else None,
            "measured": len(delivery_minutes),
        },
        "payments": dict(payments.most_common()),
        "timeline": _timeline(dated_done, start, end),
        "health": health_metrics(done, dates, weeks),
        "heatmap": heatmap,
        "by_provider": {k: _finish(v) for k, v in sorted(by_provider.items())},
        "by_service": {k: _finish(v) for k, v in sorted(by_service.items())},
        "top_merchants": [{"merchant": k[0], "provider": k[1], **_finish(v)} for k, v in top_merchants],
        "top_items": [{"name": n, "quantity": q} for n, q in items.most_common(12)],
        "by_weekday": [{"day": d, "orders": c} for d, c in zip(_WEEKDAYS, weekday)],
        "by_hour": [{"hour": h, "orders": c} for h, c in enumerate(hour)],
    }


# Platform filters: "all", a provider ("zomato", "swiggy") or "provider:service" ("swiggy:instamart").
PLATFORMS: dict[str, str] = {
    "all": "Combined",
    "zomato": "Zomato",
    "swiggy": "Swiggy",
    "swiggy:food": "Swiggy Food",
    "swiggy:instamart": "Instamart",
    "swiggy:dineout": "Dineout",
    "district": "District",
    "zepto": "Zepto",
    "blinkit": "Blinkit",
}
COMPARE_GROUPS: dict[str, list[str]] = {
    "platform": ["zomato", "swiggy", "district", "zepto", "blinkit"],
    "service": ["zomato", "swiggy:food", "swiggy:instamart", "swiggy:dineout", "district", "zepto", "blinkit"],
}


def filter_platform(orders: list[dict[str, Any]], platform: str) -> list[dict[str, Any]]:
    if platform in ("", "all"):
        return list(orders)
    if platform not in PLATFORMS:
        raise ValueError(f"unknown platform {platform!r}")
    provider, _, service = platform.partition(":")
    return [o for o in orders if o.get("provider") == provider and (not service or o.get("service") == service)]


def stats_for_range(
    data: dict[str, Any], start: date | None, end: date | None, platform: str = "all",
    timeline_bounds: tuple[date | None, date | None] | None = None,
) -> dict[str, Any]:
    """Filtered orders + metrics for a range and platform, plus the previous period's totals.

    `timeline_bounds` overrides the timeline's start/end (used by compare mode so every
    segment shares one time axis) without changing which orders are included.
    """
    scoped = filter_platform(data.get("orders", []), platform)
    orders = filter_orders(scoped, start, end)
    t_start, t_end = timeline_bounds if timeline_bounds else (start, end)
    result = {
        "platform": platform,
        "platform_label": PLATFORMS.get(platform, platform),
        "orders": orders,
        "all_count": len(scoped),
        "range": {"from": start.isoformat() if start else None, "to": end.isoformat() if end else None},
        "metrics": compute_metrics(orders, t_start, t_end),
        "previous": None,
    }
    if start and end:
        p_start, p_end = previous_period(start, end)
        prev = compute_metrics(filter_orders(scoped, p_start, p_end), p_start, p_end)["total"]
        result["previous"] = {"from": p_start.isoformat(), "to": p_end.isoformat(), **prev}
    return result


def compare_stats(data: dict[str, Any], start: date | None, end: date | None, by: str = "platform") -> dict[str, Any]:
    """Side-by-side stats for each platform/service that has any saved orders, on one shared time axis."""
    if by not in COMPARE_GROUPS:
        raise ValueError(f"unknown grouping {by!r}")
    all_orders = data.get("orders", [])
    bounds: tuple[date | None, date | None] = (start, end)
    if start is None or end is None:
        dated = [d.date() for o in filter_orders(all_orders, start, end) if (d := parse_time(o.get("placed_at", "")))]
        bounds = (start or (min(dated) if dated else None), end or (max(dated) if dated else None))
    segments = []
    for key in COMPARE_GROUPS[by]:
        if not filter_platform(all_orders, key):
            continue  # nothing ever ordered there (e.g. no Dineout history): no empty column
        segments.append({"key": key, **stats_for_range(data, start, end, key, timeline_bounds=bounds)})
    return {
        "by": by,
        "range": {"from": start.isoformat() if start else None, "to": end.isoformat() if end else None},
        "segments": segments,
        "orders": filter_orders(all_orders, start, end),
        "all_count": len(all_orders),
    }


def brand_of(label: str) -> str:
    return SERVER_BRANDS.get(label, label)
