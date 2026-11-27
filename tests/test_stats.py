"""Joint metrics math, range filtering, merging and health signals."""
from datetime import date

from food_console.stats import (
    ORDER_SCHEMA,
    compute_metrics,
    filter_orders,
    merge_orders,
    parse_time,
    previous_period,
    stats_for_range,
)


def _it(name, q=1, health="balanced", category="meal", veg="veg"):
    return {"name": name, "quantity": q, "health": health, "category": category, "veg": veg}


def _o(provider, service, merchant, total, placed_at, status="DELIVERED", items=(), oid=None, delivered_at=""):
    return {"provider": provider, "service": service, "order_id": oid or (merchant + placed_at), "placed_at": placed_at,
            "delivered_at": delivered_at, "merchant": merchant, "status": status, "total_inr": total,
            "discount_inr": 50, "delivery_fee_inr": 20, "payment_method": "upi", "address_label": "Home",
            "item_count": len(items), "items": list(items)}


ORDERS = [
    _o("zomato", "food", "Biryani House", 400, "2026-10-03T13:00:00Z", items=[_it("Biryani", 2)],
       delivered_at="2026-10-03T13:35:00Z"),
    _o("swiggy", "food", "Biryani House", 600, "2026-11-05T14:00:00Z",
       items=[_it("Burger", 1, "indulgent", "fast_food", "non_veg"), _it("Coke", 1, "indulgent", "sugary_drink")]),
    _o("swiggy", "instamart", "Instamart", 844, "2026-11-24T05:54:34.000Z",
       items=[_it("Apples", 3, "healthy", "fruit_veg"), _it("Detergent", 1, "non_food", "household")]),
    _o("zomato", "food", "Pizza Place", 999, "2026-11-01T20:00:00Z", status="CANCELLED"),
    _o("swiggy", "food", "No Date Cafe", 100, ""),
]


def test_compute_metrics_joint():
    m = compute_metrics(ORDERS)
    assert m["total"] == {"orders": 4, "spend": 1944.0, "aov": 486.0, "cancelled": 1, "all_orders": 5}
    assert m["by_provider"]["zomato"]["spend"] == 400
    assert m["by_provider"]["swiggy"] == {"orders": 3, "spend": 1544.0, "aov": 514.67}
    assert m["by_service"]["swiggy:instamart"]["orders"] == 1
    # Every merchant has 1 order per provider, so the tie is broken by spend.
    assert m["favorite"] == {"merchant": "Instamart", "provider": "swiggy", "orders": 1}
    assert m["biggest_order"]["total_inr"] == 844
    assert m["items_total"] == 8
    assert sum(map(sum, m["heatmap"])) == 3
    assert m["savings"] == {"discount": 200.0, "fees": 80.0}
    assert m["payments"] == {"UPI": 4}
    assert m["delivery"]["avg_minutes"] == 35.0 and m["delivery"]["measured"] == 1


def test_timeline_fills_empty_buckets():
    m = compute_metrics(filter_orders(ORDERS, date(2026, 11, 1), date(2026, 11, 30)), date(2026, 11, 1), date(2026, 11, 30))
    tl = m["timeline"]
    assert tl["granularity"] == "day" and len(tl["buckets"]) == 30
    assert sum(b["orders"] for b in tl["buckets"]) == 2  # cancelled order excluded
    # Longer ranges switch to weekly / monthly buckets.
    assert compute_metrics(ORDERS, date(2026, 1, 1), date(2026, 5, 1))["timeline"]["granularity"] == "week"
    assert compute_metrics(ORDERS, date(2025, 1, 1), date(2026, 12, 31))["timeline"]["granularity"] == "month"
    monthly = compute_metrics(ORDERS, date(2025, 12, 1), date(2026, 11, 30))["timeline"]["buckets"]
    assert [b["start"] for b in monthly][:2] == ["2025-12-01", "2026-01-01"] and len(monthly) == 12


def test_filter_and_previous_period():
    nov = filter_orders(ORDERS, date(2026, 11, 1), date(2026, 11, 30))
    assert {o["merchant"] for o in nov} == {"Biryani House", "Instamart", "Pizza Place"}
    assert all(o["placed_at"] for o in nov)  # undated orders only in all-time
    assert len(filter_orders(ORDERS, None, None)) == 5
    assert previous_period(date(2026, 11, 1), date(2026, 11, 30)) == (date(2026, 10, 2), date(2026, 10, 31))

    res = stats_for_range({"orders": ORDERS}, date(2026, 11, 1), date(2026, 11, 30))
    assert res["all_count"] == 5 and len(res["orders"]) == 3
    assert res["previous"]["orders"] == 1 and res["previous"]["spend"] == 400


def test_health_signals():
    h = compute_metrics(ORDERS)["health"]
    # food items: biryani 2 (balanced), burger 1 + coke 1 (indulgent), apples 3 (healthy); detergent ignored
    assert h["mix"] == {"healthy": 3, "balanced": 2, "indulgent": 2}
    factors = {f["key"]: f for f in h["factors"]}
    assert factors["sugary"]["value"] == "1" and factors["fruit_veg"]["value"] == "3"
    assert factors["indulgent"]["value"] == "29%"
    assert 0 <= h["score"] <= 100 and h["label"] in {"Great", "Good", "Fair", "Needs attention"}


def test_no_health_without_tags():
    old_style = [{**o, "items": [{"name": "x", "quantity": 1}]} for o in ORDERS]
    assert compute_metrics(old_style)["health"] is None


def test_merge_keeps_old_and_updates():
    old = {"orders": [ORDERS[0], ORDERS[1]]}
    updated = {**ORDERS[1], "status": "REFUNDED"}
    new = {"orders": [updated, ORDERS[2]], "notes": "n", "fetched_at": "t"}
    merged = merge_orders(old, new)
    assert len(merged["orders"]) == 3 and merged["added"] == 1
    assert [o for o in merged["orders"] if o["order_id"] == ORDERS[1]["order_id"]][0]["status"] == "REFUNDED"
    assert merged["notes"] == "n"


def test_empty():
    m = compute_metrics([])
    assert m["total"]["orders"] == 0 and m["total"]["aov"] == 0 and m["first_order"] is None
    assert m["favorite"] is None and m["health"] is None and m["timeline"]["buckets"] == []


def test_parse_time_local():
    assert parse_time("") is None and parse_time("garbage") is None
    assert parse_time("2026-11-24T05:54:34.000Z").tzinfo is not None


def test_schema_is_strict():
    order = ORDER_SCHEMA["properties"]["orders"]["items"]
    item = order["properties"]["items"]["items"]
    for obj in (order, item):
        assert obj["additionalProperties"] is False
        assert set(obj["required"]) == set(obj["properties"])


def test_platform_filter_and_stats():
    from food_console.stats import filter_platform
    import pytest
    assert len(filter_platform(ORDERS, "all")) == 5
    assert {o["provider"] for o in filter_platform(ORDERS, "zomato")} == {"zomato"}
    assert [o["merchant"] for o in filter_platform(ORDERS, "swiggy:instamart")] == ["Instamart"]
    with pytest.raises(ValueError):
        filter_platform(ORDERS, "ubereats")

    res = stats_for_range({"orders": ORDERS}, date(2026, 11, 1), date(2026, 11, 30), "swiggy")
    assert res["platform_label"] == "Swiggy" and res["all_count"] == 3
    assert res["metrics"]["total"]["spend"] == 1444  # Nov swiggy food 600 + instamart 844
    assert res["previous"]["orders"] == 0  # no Swiggy orders in October


def test_compare_shares_time_axis_and_skips_empty():
    from food_console.stats import compare_stats
    by_platform = compare_stats({"orders": ORDERS}, None, None, "platform")
    assert [s["key"] for s in by_platform["segments"]] == ["zomato", "swiggy"]
    starts = [[b["start"] for b in s["metrics"]["timeline"]["buckets"]] for s in by_platform["segments"]]
    # One axis across both columns; 53 days -> weekly buckets, first week starts Mon 28 Sep.
    assert starts[0] == starts[1] and starts[0][0] == "2026-09-28"
    by_service = compare_stats({"orders": ORDERS}, None, None, "service")
    assert [s["key"] for s in by_service["segments"]] == ["zomato", "swiggy:food", "swiggy:instamart"]  # no dineout
    total = sum(s["metrics"]["total"]["spend"] for s in by_service["segments"])
    assert total == compute_metrics(ORDERS)["total"]["spend"]  # segments add up to the combined view


def test_peak_hour_and_late_share():
    m = compute_metrics(ORDERS)
    assert m["peak_hour"] is not None and 0 <= m["late_night_share"] <= 1


def test_district_is_its_own_platform():
    from food_console.stats import COMPARE_GROUPS, ORDER_SCHEMA, filter_platform

    item = ORDER_SCHEMA["properties"]["orders"]["items"]["properties"]
    assert "district" in item["provider"]["enum"] and "events" in item["service"]["enum"]
    orders = [{"provider": "district", "service": "events"}, {"provider": "district", "service": "dineout"},
              {"provider": "zomato", "service": "food"}]
    assert len(filter_platform(orders, "district")) == 2
    assert "district" in COMPARE_GROUPS["platform"] and "district" in COMPARE_GROUPS["service"]
    assert "zepto" in item["provider"]["enum"] and "grocery" in item["service"]["enum"]
    assert filter_platform([{"provider": "zepto", "service": "grocery"}] + orders, "zepto") == [{"provider": "zepto", "service": "grocery"}]
    assert "zepto" in COMPARE_GROUPS["platform"]


def test_district_provider_and_pick_excludes_it():
    from types import SimpleNamespace

    from food_console.mcp_bridge import PROVIDERS, SERVER_BRANDS
    from food_console.nutrition import pick_tool_filter

    assert ("district", "district-mcp") in PROVIDERS["all"].servers and SERVER_BRANDS["district"] == "District"
    keep = pick_tool_filter(["zomato", "food"])
    assert not keep(SimpleNamespace(mcp_name="search_restaurants", server="district", read_only=True))
