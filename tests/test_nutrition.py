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
