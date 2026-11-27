"""Pick what to order by calories and protein.

Claude searches menus with read-only tools and proposes candidate dishes, each with its own
per-serving estimate. Python then does the parts that must be exact:

- nutrition the platform itself publishes (Zomato's `item_nutritional_info` / `health_info`)
  is read straight from the raw tool results by item id, so it is never a model's paraphrase;
- those listed values get a plausibility check (macros vs calories, and vs the estimate) -
  some menus carry obviously wrong numbers, e.g. a full thali at 27 kcal;
- goals, filters and ranking are plain arithmetic.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from .agent import OrderAgent
from .mcp_bridge import BridgedTool

SORTS = {
    "density": "Protein per 100 kcal",
    "protein": "Most protein",
    "kcal": "Fewest calories",
    "per_rupee": "Protein per ₹100",
}
DIETS = ("any", "veg", "non_veg")

# Preset goals the UI offers; every field can be overridden per request.
GOALS: dict[str, dict[str, Any]] = {
    "lean": {"label": "Lean & high protein", "max_kcal": 650, "min_protein": 30, "sort": "density"},
    "protein": {"label": "Max protein", "max_kcal": None, "min_protein": 40, "sort": "protein"},
    "light": {"label": "Light meal", "max_kcal": 450, "min_protein": 15, "sort": "kcal"},
    "value": {"label": "Protein on a budget", "max_kcal": 800, "min_protein": 25, "sort": "per_rupee", "max_price": 300},
}

DISH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "address_label": {"type": "string", "description": "Label of the delivery address used, e.g. Home"},
        "dishes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "provider": {"type": "string", "enum": ["zomato", "swiggy"]},
                    "service": {"type": "string", "enum": ["food", "instamart"]},
                    "merchant": {"type": "string", "description": "Restaurant or store name, exactly as returned"},
                    "merchant_id": {"type": "string", "description": "Restaurant/store id as returned (Zomato res_id)"},
                    "dish": {"type": "string", "description": "Dish name exactly as returned"},
                    "item_id": {
                        "type": "string",
                        "description": "Id used to add it to a cart, exactly as returned (Zomato variant_id, starts with v_)",
                    },
                    "price_inr": {"type": "number", "description": "Price shown to the user (discounted if given); 0 if unknown"},
                    "veg": {"type": "string", "enum": ["veg", "non_veg", "unknown"]},
                    "rating": {"type": "number", "description": "Restaurant rating; 0 if unknown"},
                    "eta": {"type": "string", "description": "Delivery time as returned, or empty"},
                    "portion": {"type": "string", "description": "What one order contains, e.g. '6 pc tikka + mint chutney'"},
                    "est_kcal": {"type": "number", "description": "Your estimate for one order as sold"},
                    "est_protein_g": {"type": "number"},
                    "est_carbs_g": {"type": "number"},
                    "est_fat_g": {"type": "number"},
                    "listed_kcal": {"type": "number", "description": "Calories the tool result states for this dish, copied exactly; -1 if none"},
                    "listed_protein_g": {"type": "number", "description": "Protein the tool result states, copied exactly; -1 if none"},
                    "why": {"type": "string", "description": "One short line: why it suits the goal, or the catch"},
                },
                "required": [
                    "provider", "service", "merchant", "merchant_id", "dish", "item_id", "price_inr", "veg", "rating",
                    "eta", "portion", "est_kcal", "est_protein_g", "est_carbs_g", "est_fat_g", "listed_kcal",
                    "listed_protein_g", "why",
                ],
                "additionalProperties": False,
            },
        },
        "notes": {"type": "string", "description": "Gaps or problems, e.g. a platform that was offline"},
    },
    "required": ["address_label", "dishes", "notes"],
    "additionalProperties": False,
}

PICK_SYSTEM = """You help the user decide what to order based on calories and protein. Using \
read-only MCP tools, find real dishes that deliver to the user's address and return candidates \
as JSON matching the given schema. Today's date is {today}.

Goal for one meal: {goal}
What they feel like: {query}
Platforms to search: {platforms}

How to search:
- Get the saved addresses first and use {address}. Pass its address id to every search and menu call.
- Search with 2-4 keywords that fit both the craving and the goal (for protein: grilled/tandoori \
chicken, kebabs, tikka, egg, fish, paneer, soya, protein bowls, salads with protein, ...). Ask for \
up to 10 results per page. Skip restaurants that are not serviceable right now.
- For the 3-5 most promising restaurants, read the menu to find better fits (Zomato: \
get_menu_items_listing with address_id; many dishes include health_info).
- Return up to {limit} dishes from several restaurants: mostly ones likely to fit the goal, plus a \
few near misses if not many fit.

For every dish:
- Copy names, ids and prices exactly from tool results. item_id is what adds it to a cart \
(Zomato variant_id, "v_..."); merchant_id is the restaurant id (Zomato res_id). Never invent \
dishes, prices or ids.
- est_*: your own realistic estimate for one order as sold - think about portion and piece \
count, cooking method, oil, gravy, cheese, and any rice, bread, sides or drink that come with it. \
Always fill these in, even when the menu lists nutrition.
- listed_kcal / listed_protein_g: only when a tool result states them for this exact dish, \
copied exactly; otherwise -1.
- If a platform fails or is offline, say so in notes and continue with the others.
- The app shows the platform's listed nutrition when it has it and your estimate otherwise, and \
does its own ranking, so keep notes to one or two sentences about gaps (offline platforms, few \
matches, filters you could not apply). Don't repeat numbers or rankings there."""

# Read-only tools the picker may use: everything read-only except order history and tracking.
_NOT_FOR_PICKING = re.compile(r"order|history|track|booking|coupon|cart|payment|status|otp", re.I)


def pick_tool_filter(servers: Iterable[str] | None = None):
    allowed = set(servers) if servers else None

    def keep(tool: BridgedTool) -> bool:
        if not tool.read_only or _NOT_FOR_PICKING.search(tool.mcp_name):
            return False
        return allowed is None or tool.server in allowed

    return keep


# ---------------------------------------------------------------- listed nutrition from raw results

@dataclass
class Listed:
    kcal: float | None = None
    protein: float | None = None
    carbs: float | None = None
    fat: float | None = None
    fibre: float | None = None
    shape: str = ""  # which field it came from, e.g. "item_nutritional_info"

    def as_dict(self) -> dict[str, Any]:
        return {"kcal": self.kcal, "protein_g": self.protein, "carbs_g": self.carbs, "fat_g": self.fat,
                "fibre_g": self.fibre, "field": self.shape}


_ID_KEYS = ("variant_id", "item_id", "id", "dish_id", "catalogue_id")
_NUTRIENT = {
    "calories": "kcal", "calorie": "kcal", "energy": "kcal", "kcal": "kcal",
    "protein": "protein", "proteins": "protein",
    "carbs": "carbs", "carbohydrate": "carbs", "carbohydrates": "carbs",
    "fat": "fat", "fats": "fat", "total_fat": "fat",
    "fibre": "fibre", "fiber": "fibre",
}


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        m = re.match(r"\s*(\d+(?:\.\d+)?)", value)
        return float(m.group(1)) if m else None
    return None


def _read_nutrition(block: Any) -> Listed | None:
    """Parse one nutrition object: flat numbers and/or a list of {name, value} entries."""
    if not isinstance(block, dict):
        return None
    found: dict[str, float] = {}
    for k, v in block.items():
        field = _NUTRIENT.get(k.lower())
        if field and _num(v) is not None:
            found[field] = _num(v)  # type: ignore[assignment]
        if isinstance(v, list):
            for entry in v:
                if not isinstance(entry, dict):
                    continue
                name = entry.get("nutrient_name") or entry.get("title") or entry.get("name") or ""
                field = _NUTRIENT.get(str(name).lower())
                value = _num(entry.get("nutrient_value", entry.get("value")))
                if field and value is not None:
                    found[field] = value
    if "kcal" not in found and "protein" not in found:
        return None
    return Listed(found.get("kcal"), found.get("protein"), found.get("carbs"), found.get("fat"), found.get("fibre"))


def _nutrition_of(item: dict[str, Any]) -> Listed | None:
    for key, value in item.items():
        k = key.lower()
        if any(w in k for w in ("nutri", "health_info", "macro")) and isinstance(value, dict):
            listed = _read_nutrition(value)
            if listed:
                listed.shape = key
                return listed
    return None


def listed_nutrition(texts: Iterable[str]) -> dict[str, Listed]:
    """Map item ids to the nutrition the platform lists for them, from raw tool results."""
    out: dict[str, Listed] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            listed = _nutrition_of(node)
            if listed:
                for k in _ID_KEYS:
                    if node.get(k) not in (None, ""):
                        out.setdefault(str(node[k]), listed)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for text in texts:
        try:
            walk(json.loads(text))
        except (json.JSONDecodeError, TypeError):
            continue
    return out


def _stated_near(texts: list[str], item_id: str, value: float) -> bool:
    """True if a raw result mentions `item_id` with `value` close by (for shapes we don't parse)."""
    if not item_id or value < 0:
        return False
    number = re.compile(r"(?<![\d.])" + re.escape(f"{value:g}") + r"(?:\.0+)?(?![\d])")
    for text in texts:
        for m in re.finditer(re.escape(item_id), text):
            if number.search(text, max(0, m.start() - 1500), m.end() + 1500):
                return True
    return False


# ---------------------------------------------------------------- checks and ranking

def plausibility(listed: Listed, est_kcal: float, price: float) -> list[str]:
    """Reasons a listed value looks wrong; empty if it looks fine."""
    issues = []
    k = listed.kcal
    if k is not None and k > 0 and None not in (listed.protein, listed.carbs, listed.fat):
        macro = 4 * listed.protein + 4 * listed.carbs + 9 * listed.fat  # type: ignore[operator]
        if abs(macro - k) / k > 0.35:
            issues.append(f"macros add up to {macro:.0f} kcal, not the listed {k:.0f}")
    if k is not None and est_kcal > 0:
        ratio = k / est_kcal
        if ratio < 0.4 or ratio > 2.5:
            issues.append(f"listed {k:.0f} kcal is far from a typical serving (~{est_kcal:.0f} kcal)")
    if k is not None and k < 120 and price >= 150:
        issues.append(f"{k:.0f} kcal is implausibly low for a ₹{price:.0f} dish")
    return issues


def _round(x: float | None, nd: int = 1) -> float | None:
    return None if x is None else round(x, nd)


def evaluate(dish: dict[str, Any], listed: Listed | None, goal: dict[str, Any], *, via: str = "") -> dict[str, Any]:
    """Pick the numbers to trust for one dish, then check it against the goal."""
    price = float(dish.get("price_inr") or 0)
    est = {"kcal": float(dish.get("est_kcal") or 0), "protein_g": float(dish.get("est_protein_g") or 0),
           "carbs_g": float(dish.get("est_carbs_g") or 0), "fat_g": float(dish.get("est_fat_g") or 0)}
    issues = plausibility(listed, est["kcal"], price) if listed else []
    if listed and listed.kcal and listed.protein is not None and not issues:
        source = "listed"
        kcal, protein = listed.kcal, listed.protein
        carbs = listed.carbs if listed.carbs is not None else est["carbs_g"]
        fat = listed.fat if listed.fat is not None else est["fat_g"]
    else:
        source = "estimate"
        kcal, protein, carbs, fat = est["kcal"], est["protein_g"], est["carbs_g"], est["fat_g"]

    # Listed values that pass the checks but differ a lot from a typical serving: still used,
    # but shown with the estimate next to them.
    caution = ""
    if source == "listed" and est["kcal"] > 0 and est["protein_g"] > 0:
        rk, rp = kcal / est["kcal"], protein / est["protein_g"]
        if not (0.65 <= rk <= 1.5) or not (0.65 <= rp <= 1.5):
            caution = f"a typical serving is closer to {est['kcal']:.0f} kcal / {est['protein_g']:.0f} g protein"

    density = protein * 100 / kcal if kcal > 0 else 0.0
    per_rupee = protein * 100 / price if price > 0 else 0.0
    checks = []
    if goal.get("max_kcal"):
        checks.append({"name": "calories", "ok": kcal <= goal["max_kcal"], "detail": f"{kcal:.0f} / ≤{goal['max_kcal']} kcal"})
    if goal.get("min_protein"):
        checks.append({"name": "protein", "ok": protein >= goal["min_protein"], "detail": f"{protein:.0f} / ≥{goal['min_protein']} g"})
    if goal.get("max_price"):
        checks.append({"name": "price", "ok": 0 < price <= goal["max_price"], "detail": f"₹{price:.0f} / ≤₹{goal['max_price']}"})
    diet = goal.get("diet") or "any"
    if diet != "any":
        checks.append({"name": "diet", "ok": dish.get("veg") == diet, "detail": dish.get("veg", "unknown").replace("_", "-")})

    return {
        **{k: dish.get(k) for k in ("provider", "service", "merchant", "merchant_id", "dish", "item_id", "veg",
                                     "rating", "eta", "portion", "why")},
        "price_inr": price,
        "kcal": round(kcal), "protein_g": _round(protein), "carbs_g": _round(carbs), "fat_g": _round(fat),
        "source": source,  # "listed" = the platform's own numbers; "estimate" = Claude's
        "listed": ({**listed.as_dict(), "via": via or "parsed"} if listed else None),
        "listed_issues": issues,
        "caution": caution,
        "estimate": est,
        "protein_per_100kcal": round(density, 1),
        "protein_per_100inr": round(per_rupee, 1),
        "protein_kcal_share": round(protein * 4 * 100 / kcal) if kcal > 0 else 0,
        "checks": checks,
        "fits": all(c["ok"] for c in checks),
    }


_SORT_KEYS = {
    "density": lambda d: (d["protein_per_100kcal"], d["protein_g"]),
    "protein": lambda d: (d["protein_g"], d["protein_per_100kcal"]),
    "kcal": lambda d: (-d["kcal"], d["protein_g"]),
    "per_rupee": lambda d: (d["protein_per_100inr"], d["protein_per_100kcal"]),
}


def rank(dishes: list[dict[str, Any]], sort: str) -> list[dict[str, Any]]:
    key = _SORT_KEYS.get(sort, _SORT_KEYS["density"])
    ranked = sorted(dishes, key=lambda d: (d["fits"], sum(c["ok"] for c in d["checks"]), *key(d)), reverse=True)
    for i, d in enumerate(ranked, 1):
        d["rank"] = i
    return ranked


def resolve_goal(params: dict[str, Any]) -> dict[str, Any]:
    """Merge a preset with explicit overrides and validate. Raises ValueError on bad input."""
    preset = params.get("goal") or "lean"
    if preset not in GOALS and preset != "custom":
        raise ValueError(f"goal must be one of: {', '.join([*GOALS, 'custom'])}")
    goal = {"max_kcal": None, "min_protein": None, "max_price": None, "sort": "density",
            **{k: v for k, v in GOALS.get(preset, {}).items() if k != "label"}}
    for k in ("max_kcal", "min_protein", "max_price"):
        if k in params:
            v = params[k]
            if v in (None, "", 0):
                goal[k] = None
            else:
                v = float(v)
                if not 0 < v < 100000:
                    raise ValueError(f"{k} out of range")
                goal[k] = int(v)
    goal["sort"] = params.get("sort") or goal["sort"]
    if goal["sort"] not in SORTS:
        raise ValueError(f"sort must be one of: {', '.join(SORTS)}")
    goal["diet"] = params.get("diet") or "any"
    if goal["diet"] not in DIETS:
        raise ValueError(f"diet must be one of: {', '.join(DIETS)}")
    goal["preset"] = preset
    return goal


def describe_goal(goal: dict[str, Any]) -> str:
    parts = []
    if goal.get("max_kcal"):
        parts.append(f"at most {goal['max_kcal']} kcal")
    if goal.get("min_protein"):
        parts.append(f"at least {goal['min_protein']} g protein")
    if goal.get("max_price"):
        parts.append(f"at most ₹{goal['max_price']}")
    if goal.get("diet") == "veg":
        parts.append("vegetarian only")
    elif goal.get("diet") == "non_veg":
        parts.append("non-vegetarian")
    parts.append(f"prefer {SORTS[goal['sort']].lower()}")
    return ", ".join(parts)


def build_result(raw: dict[str, Any], texts: list[str], goal: dict[str, Any]) -> dict[str, Any]:
    """Turn Claude's candidates plus the raw tool results into a ranked, checked list."""
    listed_by_id = listed_nutrition(texts)
    seen: set[tuple[str, str]] = set()
    dishes = []
    for d in raw.get("dishes", []):
        key = (str(d.get("item_id") or ""), str(d.get("dish") or "").lower())
        if key in seen:
            continue
        seen.add(key)
        listed, via = listed_by_id.get(str(d.get("item_id") or "")), "parsed"
        if listed is None and d.get("listed_kcal", -1) >= 0 and _stated_near(texts, str(d.get("item_id") or ""), d["listed_kcal"]):
            # A shape the parser doesn't know, but the number really is in the tool output.
            protein = d.get("listed_protein_g", -1)
            ok_protein = protein >= 0 and _stated_near(texts, str(d.get("item_id") or ""), protein)
            listed, via = Listed(kcal=d["listed_kcal"], protein=protein if ok_protein else None, shape="tool text"), "matched"
        dishes.append(evaluate(d, listed, goal, via=via))
    ranked = rank(dishes, goal["sort"])
    return {
        "dishes": ranked,
        "fits": sum(d["fits"] for d in ranked),
        "listed_count": sum(d["source"] == "listed" for d in ranked),
        "address_label": raw.get("address_label", ""),
        "notes": raw.get("notes", ""),
    }


async def pick_dishes(
    agent: OrderAgent, goal: dict[str, Any], *, query: str = "", platforms: str = "Zomato and Swiggy",
    address: str = "", limit: int = 20,
) -> dict[str, Any]:
    """Run the picker agent and return ranked dishes with checked nutrition."""
    from datetime import date

    agent.system = PICK_SYSTEM.format(
        today=date.today().isoformat(),
        goal=describe_goal(goal),
        query=query.strip() or "nothing specific - choose keywords that suit the goal",
        platforms=platforms,
        address=f'the address labelled "{address}"' if address else "the first (default) address",
        limit=limit,
    )
    agent.tool_log = []
    text = await agent.send("Find what I should order.", output_format={"type": "json_schema", "schema": DISH_SCHEMA},
                            max_tokens=32000)
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"picker did not return JSON: {text[:300]}") from exc
    texts = [entry["text"] for entry in agent.tool_log]
    result = build_result(raw, texts, goal)
    return {**result, "goal": goal, "query": query, "searched_at": datetime.now(timezone.utc).isoformat(),
            "tool_calls": len(texts)}
