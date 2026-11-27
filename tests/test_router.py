"""Model router: fixed task routes, classifier tiers, the in-conversation ratchet, overrides."""
import asyncio
import json
from types import SimpleNamespace

from food_console.router import DEFAULT_MODELS, Router
from food_console.usage import aggregate, round_cost, summarize


class FakeClient:
    """Stands in for AsyncAnthropic: returns a canned classifier answer, records requests."""

    def __init__(self, tier="standard", fail=False):
        self.calls = []

        async def create(**kw):
            self.calls.append(kw)
            if fail:
                raise RuntimeError("network down")
            return SimpleNamespace(model=kw["model"], content=[SimpleNamespace(type="text", text=json.dumps({"tier": tier, "reason": "test"}))],
                                   usage=SimpleNamespace(input_tokens=300, output_tokens=20, cache_read_input_tokens=0,
                                                         cache_creation_input_tokens=0))

        self.beta = SimpleNamespace(messages=SimpleNamespace(create=create))


def choose(router, client, **kw):
    return asyncio.run(router.choose(client, **{"kind": "chat", "text": "hi", **kw}))


def test_tasks_have_fixed_routes_without_a_classifier_call():
    client = FakeClient()
    for kind in ("pick", "stats", "cli:pick"):
        d = choose(Router(), client, kind=kind)
        assert (d.tier, d.model, d.source) == ("standard", DEFAULT_MODELS["standard"], "task")
    assert client.calls == []


def test_classifier_picks_the_tier_and_is_cheap():
    client = FakeClient("light")
    d = choose(Router(), client, text="list my saved addresses")
    assert (d.tier, d.model, d.effort, d.source) == ("light", "claude-haiku-5-5", "low", "router")
    (call,) = client.calls
    assert call["model"] == "claude-haiku-5-5" and call["max_tokens"] <= 400 and "tools" not in call
    assert "fallbacks" not in call                       # Haiku 5.5 has no server-side fallback
    assert d.usage["router"] and d.usage["input"] == 300


def test_tier_only_goes_up_within_a_conversation():
    d = choose(Router(), FakeClient("light"), text="ok", floor="complex")
    assert (d.tier, d.model, d.source) == ("complex", "claude-opus-5-5", "ratchet")
    up = choose(Router(), FakeClient("complex"), text="analyse my spending", floor="light")
    assert up.tier == "complex"


def test_overrides_failures_and_off_switch(monkeypatch):
    d = choose(Router(), FakeClient("complex"), override="haiku", floor="complex")
    assert (d.model, d.source) == ("claude-haiku-5-5", "fixed")   # your explicit choice wins
    failed = choose(Router(), FakeClient(fail=True), text="anything")
    assert (failed.tier, failed.source) == ("standard", "fallback") and failed.usage is None
    monkeypatch.setenv("FOOD_ROUTER", "off")
    monkeypatch.setenv("ZOMATO_CHAT_MODEL", "claude-opus-5-5")
    off = choose(Router.from_env(), FakeClient())
    assert (off.model, off.source) == ("claude-opus-5-5", "off")
    monkeypatch.setenv("FOOD_ROUTER", "auto")
    monkeypatch.setenv("FOOD_MODEL_STANDARD", "claude-opus-5-5")
    assert choose(Router.from_env(), FakeClient(), kind="pick").model == "claude-opus-5-5"


def test_agent_sends_fallbacks_only_to_opus():
    from food_console.agent import OrderAgent

    sent = []

    async def create(**kw):
        sent.append(kw)
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="ok")], model=kw["model"],
                               usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0,
                                                     cache_creation_input_tokens=0))

    agent = OrderAgent(SimpleNamespace(tools={}), approve=None, on_event=lambda e: None)
    agent.client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
    for model in ("claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"):
        agent.model = model
        asyncio.run(agent._create(None, 1000))
    assert [("fallbacks" in k, k["model"]) for k in sent] == [
        (False, "claude-sonnet-5-5"), (True, "claude-opus-5-5"), (False, "claude-haiku-5-5")]


def test_agent_routes_each_run_and_records_the_router_call():
    from food_console.agent import OrderAgent

    async def create(**kw):
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="done")], model=kw["model"],
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=50, cache_read_input_tokens=0,
                                                     cache_creation_input_tokens=0))

    events, runs = [], []
    agent = OrderAgent(SimpleNamespace(tools={}), approve=None, on_event=events.append)
    agent.router, agent.on_usage = Router(), runs.append
    agent.client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
    asyncio.run(agent.send("list my addresses"))   # fake classifier echoes the requested model's tier? -> standard
    assert any(e["type"] == "model" for e in events)
    run = runs[0]
    assert run["model"] == agent.model and run["tier"] == agent.tier


def test_pricing_per_model_and_router_cost_counted():
    assert round_cost({"model": "claude-sonnet-5-5", "input": 1_000_000}) == 2.0
    assert round_cost({"model": "claude-haiku-5-5", "input": 100_000}) == 0.01
    assert round_cost({"model": "claude-haiku-5-5", "input": 200_000}) == 0.1   # long-prompt rate
    run = {"kind": "chat", "label": "x", "started_at": 1_790_000_000.0, "model": "claude-sonnet-5-5", "tools_offered": 1,
           "rounds": [{"model": "claude-sonnet-5-5", "input": 1000, "output": 100, "cache_read": 0, "cache_write": 0}],
           "router": {"model": "claude-haiku-5-5", "input": 300, "output": 20, "cache_read": 0, "cache_write": 0, "router": True},
           "tools": []}
    s = summarize(run)
    assert s["api_calls"] == 1 and s["input"] == 1300 and s["models"] == ["claude-sonnet-5-5"]
    assert abs(s["cost_usd"] - (1000 * 2 + 100 * 10 + 300 * 0.1 + 20 * 0.5) / 1e6) <= 5e-5  # rounded to $0.0001
    assert set(aggregate([s])["by_model"]) == {"claude-sonnet-5-5"}
