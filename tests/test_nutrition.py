"""Dish picker: listed-nutrition parsing, plausibility checks, goals and ranking."""
import json
from types import SimpleNamespace

import pytest

from food_console.nutrition import (
    build_result,
    describe_goal,
    listed_nutrition,
    pick_tool_filter,
    plausibility,
    rank,
    resolve_goal,
    Listed,
)

# Trimmed copies of the two shapes Zomato returns.
SEARCH = json.dumps({"results": [{"res_id": 1, "name": "Grill Co", "items": [{
    "name": "Grilled BBQ Chicken", "price": 319.0, "variant_id": "v_1", "is_veg": False,
    "item_nutritional_info": {"calories": 380, "healthy_score_value": 77, "macro_nutrients": [
        {"nutrient_name": "protein", "nutrient_value": 35}, {"nutrient_name": "carbohydrate", "nutrient_value": 15},
        {"nutrient_name": "fat", "nutrient_value": 20}, {"nutrient_name": "fiber", "nutrient_value": 2}]}}]}]})
LISTING = json.dumps({"data": {"res_id": 2, "item_mappings": [], "items": [
    {"name": "Paneer Thali", "variant_id": "v_2", "discounted_price": 259.0, "health_info": {"score": "Low",
        "item_health_info": [{"title": "protein", "value": "1.5", "unit": "g"}, {"title": "carbs", "value": "4", "unit": "g"},
                             {"title": "fat", "value": "0.6", "unit": "g"}, {"title": "Calories", "value": "27.4", "unit": "kcal"}]}},
    {"name": "Ice cream", "variant_id": "v_3", "health_info": None},
]}})


def dish(item_id, name, kcal, protein, price=250, veg="non_veg", **kw):
    return {"provider": "zomato", "service": "food", "merchant": "M", "merchant_id": "1", "dish": name, "item_id": item_id,
            "price_inr": price, "veg": veg, "rating": 4.2, "eta": "", "portion": "", "est_kcal": kcal,
            "est_protein_g": protein, "est_carbs_g": 30, "est_fat_g": 15, "listed_kcal": -1, "listed_protein_g": -1,
            "why": "", **kw}


def test_parses_both_zomato_shapes():
    found = listed_nutrition([SEARCH, LISTING, "not json"])
    assert found["v_1"].kcal == 380 and found["v_1"].protein == 35 and found["v_1"].carbs == 15
    assert found["v_2"].kcal == 27.4 and found["v_2"].protein == 1.5
    assert "v_3" not in found  # health_info: null


def test_plausibility_flags_broken_menu_values():
    assert plausibility(Listed(380, 35, 15, 20), est_kcal=420, price=319) == []
    issues = plausibility(Listed(27.4, 1.5, 4, 0.6), est_kcal=850, price=259)
    assert any("typical serving" in i for i in issues) and any("implausibly low" in i for i in issues)
    # Macros that don't add up to the stated calories.
    assert any("macros" in i for i in plausibility(Listed(200, 40, 40, 20), est_kcal=500, price=100))


def test_listed_values_win_unless_implausible():
    goal = resolve_goal({"goal": "lean"})
    r = build_result({"dishes": [dish("v_1", "Grilled BBQ Chicken", 450, 30), dish("v_2", "Paneer Thali", 850, 28, veg="veg")]},
                     [SEARCH, LISTING], goal)
    by = {d["item_id"]: d for d in r["dishes"]}
    assert by["v_1"]["source"] == "listed" and by["v_1"]["kcal"] == 380 and by["v_1"]["protein_g"] == 35
    assert by["v_2"]["source"] == "estimate" and by["v_2"]["kcal"] == 850 and by["v_2"]["listed_issues"]
    assert by["v_1"]["fits"] and not by["v_2"]["fits"]
    assert r["dishes"][0]["item_id"] == "v_1" and r["listed_count"] == 1
    assert by["v_1"]["caution"] == ""  # 380 vs ~450 kcal estimate: close enough


def test_listed_but_far_from_estimate_gets_a_caution():
    r = build_result({"dishes": [dish("v_1", "Grilled BBQ Chicken", 700, 70)]}, [SEARCH], resolve_goal({"goal": "lean"}))
    d = r["dishes"][0]
    assert d["source"] == "listed" and d["kcal"] == 380 and "700 kcal" in d["caution"]


def test_model_claimed_values_need_to_be_in_the_tool_output():
    goal = resolve_goal({"goal": "custom"})
    raw_text = '{"menu": [{"code": "x9", "info": "Energy 410 kcal, protein 33 g"}]}'
    honest = dish("x9", "Tikka", 450, 30, listed_kcal=410, listed_protein_g=33)
    made_up = dish("x9", "Tikka 2", 450, 30, listed_kcal=999, listed_protein_g=33)
    r = build_result({"dishes": [honest, made_up]}, [raw_text], goal)
    by = {d["dish"]: d for d in r["dishes"]}
    assert by["Tikka"]["source"] == "listed" and by["Tikka"]["listed"]["via"] == "matched"
    assert by["Tikka 2"]["source"] == "estimate" and by["Tikka 2"]["listed"] is None


def test_goal_checks_and_ranking():
    goal = resolve_goal({"goal": "custom", "max_kcal": 600, "min_protein": 30, "max_price": 300, "diet": "veg", "sort": "density"})
    r = build_result({"dishes": [
        dish("a", "Soya chaap", 500, 40, price=280, veg="veg"),       # fits, 8 g/100 kcal
        dish("b", "Paneer tikka", 450, 30, price=290, veg="veg"),     # fits, 6.7
        dish("c", "Chicken breast", 350, 45, price=250),              # non-veg: misses diet
        dish("d", "Thali", 900, 25, price=200, veg="veg"),            # misses kcal and protein
        dish("a", "Soya chaap", 500, 40, price=280, veg="veg"),       # duplicate, dropped
    ]}, [], goal)
    assert [d["item_id"] for d in r["dishes"]] == ["a", "b", "c", "d"]
    assert r["fits"] == 2 and [d["rank"] for d in r["dishes"]] == [1, 2, 3, 4]
    c = r["dishes"][2]
    assert [ch["name"] for ch in c["checks"] if not ch["ok"]] == ["diet"]
    assert c["protein_per_100kcal"] == round(45 * 100 / 350, 1)
    by_protein = rank(r["dishes"], "protein")
    assert by_protein[0]["item_id"] == "a"  # fits first, then most protein


def test_resolve_goal_validation_and_overrides():
    g = resolve_goal({"goal": "lean", "max_kcal": "500", "max_price": ""})
    assert g["max_kcal"] == 500 and g["min_protein"] == 30 and g["max_price"] is None and g["sort"] == "density"
    assert "at most 500 kcal" in describe_goal(g)
    for bad in ({"goal": "nope"}, {"sort": "x"}, {"diet": "vegan"}, {"max_kcal": -5}, {"min_protein": "abc"}):
        with pytest.raises(ValueError):
            resolve_goal(bad)


def test_pick_tools_are_read_only_search_tools():
    def tool(name, server="zomato", ro=True):
        return SimpleNamespace(mcp_name=name, server=server, read_only=ro)
    keep = pick_tool_filter(["zomato"])
    assert keep(tool("get_restaurants_for_keyword")) and keep(tool("get_saved_addresses_for_user"))
    assert not keep(tool("create_cart", ro=False))
    assert not keep(tool("get_cart"))  # read-only but not needed to pick
    assert not keep(tool("get_order_history"))
    assert not keep(tool("search_restaurants", server="food"))  # other platform


def test_parse_saved_addresses_from_both_platforms():
    from food_console.nutrition import describe_address, parse_addresses

    zomato = json.dumps({"addresses": [{"address_id": "111", "label": "Home", "location_name": "12, MG Road, Bengaluru"},
                                       {"address_id": "", "label": "broken"}], "success": True, "message": None})
    swiggy = ("Found 2 saved addresses (page 1 of 1, showing 2):\n"
              "1. [home blr] A Person: 12, MG Road, Bengaluru 560001, India (id: d4abc12__xyz)\n"
              "2. [Office] B Person: Tower 2, Outer Ring Road, Bengaluru (id: e9f00__q1)\n")
    z, s = parse_addresses("zomato", zomato), parse_addresses("food", swiggy)
    assert z == [{"server": "zomato", "id": "111", "label": "Home", "text": "12, MG Road, Bengaluru"}]
    assert [(a["id"], a["label"]) for a in s] == [("d4abc12__xyz", "home blr"), ("e9f00__q1", "Office")]
    assert s[0]["text"] == "12, MG Road, Bengaluru 560001, India"
    assert parse_addresses("food", "no addresses") == []

    hint = describe_address(z[0], ["zomato", "food"])
    assert "address id is 111" in hint and "Swiggy Food" in hint and "same location" in hint
    assert "same location" not in describe_address(z[0], ["zomato"])  # single platform: nothing to match


def test_restaurant_distances_from_both_platforms():
    from food_console.nutrition import listed_distances, listed_nutrition

    zomato = json.dumps({"results": [{"res_id": 21122812, "name": "BOX8", "distance": 2.4, "eta": "25-30 mins",
                                      "items": [{"variant_id": "v_9", "name": "x"}]}]})
    # Swiggy: JSON followed by a text note.
    swiggy = json.dumps({"restaurants": [{"id": "52989", "name": "R", "distanceKm": 0.8, "deliveryTimeMinutes": 31},
                                         {"id": "426", "name": "S", "distanceKm": 1.8}], "dishes": []}) + \
        "\n\n⚠️ A rich UI widget may be shown to the user with this data."
    km = listed_distances([zomato, swiggy, "not json"])
    assert km == {"21122812": 2.4, "52989": 0.8, "426": 1.8}
    # The same trailing-note format must not hide nutrition either.
    swiggy_dish = json.dumps({"dishes": [{"id": "d1", "nutrition": {"calories": 300, "protein": 30}}]}) + "\n\nnote"
    assert listed_nutrition([swiggy_dish])["d1"].kcal == 300


def test_distance_attached_to_dishes_and_claims_checked():
    goal = resolve_goal({"goal": "custom"})
    tool = json.dumps({"restaurants": [{"id": "52989", "distanceKm": 0.8}]})
    r = build_result({"dishes": [
        dish("a", "Tikka", 400, 40, merchant_id="52989"),                        # distance from tool data
        dish("b", "Kebab", 400, 40, merchant_id="777", distance_km=0.3),         # claimed, not in any result
        dish("c", "Wrap", 400, 40, merchant_id="888", distance_km=-1),
    ]}, [tool], goal)
    by = {d["item_id"]: d for d in r["dishes"]}
    assert by["a"]["distance_km"] == 0.8 and by["b"]["distance_km"] is None and by["c"]["distance_km"] is None


def test_craving_is_the_only_search_keyword():
    from types import SimpleNamespace

    from food_console.nutrition import craving_guard

    def tool(*params):
        return SimpleNamespace(tool=SimpleNamespace(input_schema={"properties": {p: {"type": "string"} for p in params}}))

    guard = craving_guard("Paneer Tikka")
    zomato_search, swiggy_search, menu = tool("keyword", "address_id"), tool("query", "addressId"), tool("res_id", "address_id")
    assert guard(zomato_search, {"keyword": "paneer tikka", "address_id": "1"}) is None     # case-insensitive
    assert guard(swiggy_search, {"query": "Paneer  Tikka!"}) is None                         # spacing/punctuation
    assert "only" in guard(zomato_search, {"keyword": "grilled chicken"})
    assert "only" in guard(swiggy_search, {"query": "paneer tikka high protein"})            # extra words refused
    assert "Refused" in guard(zomato_search, {"address_id": "1"})                            # no keyword = recommendations
    assert guard(menu, {"res_id": 5, "address_id": "1"}) is None                             # not a search tool


def test_dishes_marked_off_craving_are_dropped():
    r = build_result({"dishes": [dish("a", "Paneer Tikka", 400, 30, matches_craving=True),
                                 dish("b", "Chicken Tikka", 350, 40, matches_craving=False),
                                 dish("c", "Paneer Bhurji", 450, 28)]}, [], resolve_goal({"goal": "lean"}))
    assert [d["item_id"] for d in r["dishes"]] == ["a", "c"] and r["dropped_off_craving"] == 1


def test_treat_ignores_calories_and_protein():
    from food_console.nutrition import search_rule

    goal = resolve_goal({"goal": "treat", "max_kcal": 500, "min_protein": 40, "max_price": 400})
    assert goal["max_kcal"] is None and goal["min_protein"] is None and goal["max_price"] == 400
    assert goal["sort"] == "rating" and "no calorie or protein limits" in describe_goal(goal)
    r = build_result({"dishes": [
        dish("a", "Loaded Pizza", 1400, 30, price=380, rating=4.6),
        dish("b", "Brownie Sundae", 900, 8, price=250, rating=4.8),
        dish("c", "Pricey Platter", 1800, 60, price=650, rating=4.9),    # over the price limit
    ]}, [], goal)
    assert [d["item_id"] for d in r["dishes"]] == ["b", "a", "c"]     # fits first, then best rated
    assert r["fits"] == 2 and [c["name"] for c in r["dishes"][0]["checks"]] == ["price"]
    assert rank(r["dishes"], "indulgent")[0]["item_id"] == "a"         # most calories among those that fit
    assert "pizza" in search_rule("", goal) and "paneer" in search_rule("paneer", goal)   # a craving still wins
