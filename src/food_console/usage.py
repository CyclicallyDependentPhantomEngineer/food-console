"""Token accounting per run (one chat message, one Pick search, one Stats refresh).

Costs are estimates at Claude API list prices per million tokens. With the `ant` sign-in your
actual billing may differ (e.g. a plan instead of pay-as-you-go), so treat them as a guide
for what is expensive, not as an invoice.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

# USD per million tokens (Claude API list prices). cache_write = 5-minute TTL = 1.25x input.
# Haiku 5.5's rates change above a 100K-token prompt ("long"); cache reads ~0.1x and writes
# 1.25x of its input rate per the prompt-caching docs.
PRICES: dict[str, dict[str, float]] = {
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_read": 0.20, "cache_write": 5.0},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_read": 0.10, "cache_write": 2.5},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125},
    "claude-haiku-5-5:long": {"input": 0.50, "output": 2.50, "cache_read": 0.05, "cache_write": 0.625},
}
FIELDS = ("input", "output", "cache_read", "cache_write")


def price_for(model: str | None) -> dict[str, float] | None:
    if not model:
        return None
    return PRICES.get(model) or next((p for m, p in PRICES.items() if model.startswith(m)), None)


def round_cost(r: dict[str, Any]) -> float | None:
    model = r.get("model") or ""
    prompt = r.get("input", 0) + r.get("cache_read", 0) + r.get("cache_write", 0)
    if model.startswith("claude-haiku-5-5") and prompt > 100_000:
        model = "claude-haiku-5-5:long"
    p = PRICES.get(model) or price_for(model)
    if p is None:
        return None
    return sum(r.get(f, 0) * p[f] for f in FIELDS) / 1_000_000


def summarize(run: dict[str, Any]) -> dict[str, Any]:
    """Totals, cost and where the tokens went, for one recorded run."""
    rounds = run.get("rounds", [])
    router = run.get("router")  # the classifier call that picked the model, if any
    billed = rounds + ([router] if router else [])
    totals = {f: sum(r.get(f, 0) for r in billed) for f in FIELDS}
    costs = [round_cost(r) for r in billed]
    prompt = totals["input"] + totals["cache_read"] + totals["cache_write"]
    tool_chars = sum(t.get("chars", 0) for t in run.get("tools", []))
    # A tool result is re-sent with every later API call of the run (as part of the history).
    resent = 0
    for t in run.get("tools", []):
        later = max(0, len(rounds) - t.get("round", len(rounds)))
        resent += t.get("chars", 0) * later
    biggest = sorted(run.get("tools", []), key=lambda t: t.get("chars", 0), reverse=True)[:5]
    return {
        **{k: v for k, v in run.items() if k not in ("rounds", "tools", "router")},
        **totals,
        "prompt_tokens": prompt,
        "total_tokens": prompt + totals["output"],
        "api_calls": len(rounds),
        "models": sorted({r.get("model") for r in rounds if r.get("model")}),
        "router_cost_usd": round_cost(router) if router else None,
        "tool_calls": len(run.get("tools", [])),
        "cache_share": round(totals["cache_read"] * 100 / prompt) if prompt else 0,
        "cost_usd": None if any(c is None for c in costs) else round(sum(costs), 4),
        "tool_result_chars": tool_chars,
        "tool_result_chars_resent": resent,
        "biggest_tools": biggest,
        "rounds": [{**r, "cost_usd": round_cost(r), "prompt": r.get("input", 0) + r.get("cache_read", 0) + r.get("cache_write", 0)}
                   for r in rounds],
        "tools": run.get("tools", []),
    }


def aggregate(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Totals, per-kind and per-day figures over summarized runs."""
    def blank() -> dict[str, float]:
        return {"runs": 0, "total_tokens": 0, "prompt_tokens": 0, "cost_usd": 0.0, **{f: 0 for f in FIELDS}}

    total, by_kind, by_day, by_model = blank(), defaultdict(blank), defaultdict(blank), defaultdict(blank)
    for r in runs:
        day = datetime.fromtimestamp(r["started_at"]).date().isoformat()
        for bucket in (total, by_kind[r.get("kind", "chat")], by_day[day], by_model[r.get("model") or "unknown"]):
            bucket["runs"] += 1
            for f in (*FIELDS, "total_tokens", "prompt_tokens"):
                bucket[f] += r.get(f, 0)
            bucket["cost_usd"] += r.get("cost_usd") or 0
    for b in (total, *by_kind.values(), *by_day.values(), *by_model.values()):
        b["cost_usd"] = round(b["cost_usd"], 4)
    total["cache_share"] = round(total["cache_read"] * 100 / total["prompt_tokens"]) if total["prompt_tokens"] else 0
    total["avg_tokens"] = round(total["total_tokens"] / total["runs"]) if total["runs"] else 0
    return {"total": total, "by_kind": dict(by_kind), "by_day": dict(sorted(by_day.items())), "by_model": dict(by_model)}
