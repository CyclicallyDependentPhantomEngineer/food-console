"""Token accounting: per-run recording in the agent, cost maths, encrypted storage."""
import asyncio
from types import SimpleNamespace

from cryptography.fernet import Fernet

from food_console.store import Store
from food_console.usage import aggregate, summarize


def run(**over):
    base = {"kind": "chat", "label": "order biryani", "started_at": 1_790_000_000.0, "model": "claude-opus-5-5",
            "tools_offered": 120, "ms": 4000,
            "rounds": [
                {"model": "claude-opus-5-5", "input": 2000, "output": 300, "cache_read": 0, "cache_write": 30000, "stop": "tool_use",
                 "tool_uses": ["food_search_menu"]},
                {"model": "claude-opus-5-5", "input": 500, "output": 200, "cache_read": 32000, "cache_write": 4000, "stop": "end_turn",
                 "tool_uses": []},
            ],
            "tools": [{"tool": "food:search_menu", "chars": 8000, "source": "live", "is_error": False, "round": 1}]}
    return {**base, **over}


def test_summary_and_cost_at_list_prices():
    s = summarize(run())
    assert (s["input"], s["output"], s["cache_read"], s["cache_write"]) == (2500, 500, 32000, 34000)
    assert s["prompt_tokens"] == 68500 and s["total_tokens"] == 69000 and s["api_calls"] == 2
    # $4 in, $20 out, $0.20 cache read, $5 cache write per million tokens
    expected = (2500 * 4 + 500 * 20 + 32000 * 0.20 + 34000 * 5) / 1e6
    assert abs(s["cost_usd"] - expected) < 1e-4
    assert s["cache_share"] == round(32000 * 100 / 68500)
    assert s["tool_result_chars_resent"] == 8000          # added after call 1, re-sent with call 2
    assert summarize(run(rounds=[{**run()["rounds"][0], "model": "some-other-model"}]))["cost_usd"] is None


def test_aggregate_by_kind_and_day():
    runs = [summarize(run()), summarize(run(kind="pick")), summarize(run(started_at=1_790_100_000.0))]
    a = aggregate(runs)
    assert a["total"]["runs"] == 3 and set(a["by_kind"]) == {"chat", "pick"} and len(a["by_day"]) == 2
    assert a["total"]["total_tokens"] == 3 * 69000


def test_store_keeps_runs_encrypted(tmp_path):
    db = Store(tmp_path / "food.db", key=Fernet.generate_key())
    db.save_usage(run(label="order paneer from my secret place"))
    assert b"secret place" not in (tmp_path / "food.db").read_bytes()
    got = db.load_usage()
    assert len(got) == 1 and got[0]["label"] == "order paneer from my secret place"
    assert db.load_usage(since=1_790_000_001.0) == []
    assert db.clear("usage") == 1 and db.load_usage() == []


def test_agent_records_each_api_call():
    from food_console.agent import OrderAgent

    def reply(stop, content, inp, out, read, write):
        return SimpleNamespace(stop_reason=stop, content=content, model="claude-opus-5-5",
                               usage=SimpleNamespace(input_tokens=inp, output_tokens=out, cache_read_input_tokens=read,
                                                     cache_creation_input_tokens=write))

    replies = [
        reply("tool_use", [SimpleNamespace(type="tool_use", id="t1", name="get_menu", input={})], 1000, 50, 0, 9000),
        reply("end_turn", [SimpleNamespace(type="text", text="Here you go")], 200, 80, 9000, 400),
    ]
    agent = OrderAgent.__new__(OrderAgent)
    agent.messages, agent.usage, agent.on_event = [], {"input": 0, "output": 0, "cache_read": 0}, lambda e: None
    agent.usage_kind, agent.usage_label, agent._run, agent.tool_filter = "chat", None, None, None
    agent.mcp = SimpleNamespace(tools={})
    agent.router, agent.model_override, agent.model, agent.effort, agent.tier = None, None, "claude-opus-5-5", "medium", None
    recorded = []
    agent.on_usage = recorded.append

    async def fake_create(output_format, max_tokens):
        return replies.pop(0)

    async def fake_tool(block):
        agent._run["tools"].append({"tool": "x:get_menu", "chars": 1234, "source": "live", "is_error": False,
                                    "round": len(agent._run["rounds"])})
        return {"type": "tool_result", "tool_use_id": block.id, "content": "menu"}

    agent._create, agent._run_tool = fake_create, fake_tool
    assert asyncio.run(agent.send("what's on the menu?")) == "Here you go"
    (r,) = recorded
    assert r["label"] == "what's on the menu?" and len(r["rounds"]) == 2 and r["tools"][0]["chars"] == 1234
    assert [x["cache_write"] for x in r["rounds"]] == [9000, 400] and r["ms"] >= 0
