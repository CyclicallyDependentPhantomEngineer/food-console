"""Web UI guards: token, Host header, and confirmation before account-changing tools."""
import argparse
import asyncio
import json
import sys
import threading
from pathlib import Path

import pytest
from mcp import StdioServerParameters
from starlette.testclient import TestClient

from food_console.mcp_bridge import MCPBridge
from food_console.web import AppState, build_app

FAKE = StdioServerParameters(command=sys.executable, args=[str(Path(__file__).with_name("fake_server.py"))])
PORT = 8765


@pytest.fixture()
def client():
    """App wired to a fake MCP server; the MCP session lives on a background event loop."""
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    bridge = MCPBridge([("food", FAKE), ("zomato", FAKE)])
    ready, stop = threading.Event(), asyncio.Event()

    async def hold():  # enter and exit the bridge in the same task (anyio requires it)
        async with bridge:
            ready.set()
            await stop.wait()

    held = asyncio.run_coroutine_threadsafe(hold(), loop)
    assert ready.wait(30)

    state = AppState(argparse.Namespace(port=PORT, profile=None, history_limit=5))
    state.mcp = bridge
    state.chat = state.new_agent()
    # Route tool calls onto the loop that owns the MCP session (the test client runs its own loop).
    real_call_ex = bridge.call_ex
    bridge.call_ex = lambda name, args: asyncio.wrap_future(asyncio.run_coroutine_threadsafe(real_call_ex(name, args), loop))

    with TestClient(build_app(state), base_url=f"http://127.0.0.1:{PORT}") as c:
        r = c.get(f"/login?t={state.login_token}", follow_redirects=False)
        assert r.status_code == 303
        c.headers["X-Food-Console"] = "1"
        c.state = state
        yield c
    loop.call_soon_threadsafe(stop.set)
    held.result(30)
    loop.call_soon_threadsafe(loop.stop)


def test_session_required(client):
    from starlette.testclient import TestClient
    assert client.get("/api/status").status_code == 200
    # A fresh browser without the session cookie gets nothing.
    anon = TestClient(client.app, base_url=f"http://127.0.0.1:{PORT}")
    assert anon.get("/api/status", headers={"X-Food-Console": "1"}).status_code == 401
    assert anon.get("/", follow_redirects=False).status_code == 401  # signed-out page


def test_custom_header_required(client):
    assert client.get("/api/status", headers={"X-Food-Console": ""}).status_code == 403


def test_login_link_is_one_time_and_cookie_is_locked_down(client):
    from starlette.testclient import TestClient
    state = client.state
    fresh = TestClient(client.app, base_url=f"http://127.0.0.1:{PORT}")
    link = state.login_token
    r = fresh.get(f"/login?t={link}", follow_redirects=False)
    cookie = r.headers["set-cookie"].lower()
    assert r.status_code == 303 and "httponly" in cookie and "samesite=strict" in cookie
    assert r.headers["referrer-policy"] == "no-referrer"
    # The same link (e.g. from browser history) no longer works; the rotated one does.
    replay = TestClient(client.app, base_url=f"http://127.0.0.1:{PORT}")
    assert replay.get(f"/login?t={link}", follow_redirects=False).status_code == 403
    assert replay.get(f"/login?t={state.login_token}", follow_redirects=False).status_code == 303
    assert replay.get("/login?t=", follow_redirects=False).status_code == 403


def test_bad_host_rejected(client):
    assert client.get("/api/status", headers={"Host": "evil.example:8765"}).status_code == 421


def test_index_served_when_signed_in(client):
    r = client.get("/")
    assert r.status_code == 200 and "Food Console" in r.text
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


def test_tools_listing_has_brands(client):
    tools = {t["name"]: t for t in client.get("/api/tools").json()}
    assert tools["food_get_order_history"]["brand"] == "Swiggy"
    assert tools["zomato_place_order"]["brand"] == "Zomato"
    assert tools["food_get_order_history"]["needs_confirm"] is False
    # Strict mode in the UI: anything not read-only needs confirmation.
    assert tools["zomato_place_order"]["needs_confirm"] is True


def test_read_only_tool_runs(client):
    r = client.post("/api/tools/food_get_order_history", json={"args": {"limit": 1}})
    assert r.status_code == 200 and "Biryani House" in r.json()["result"]


def test_changing_tool_needs_confirm(client):
    assert client.post("/api/tools/zomato_place_order", json={"args": {"cart_id": "c1"}}).status_code == 409
    r = client.post("/api/tools/zomato_place_order", json={"args": {"cart_id": "c1"}, "confirm": True})
    assert r.json() == {"result": "placed c1", "is_error": False, "source": "live", "cached_at": None}


def test_approve_unknown_id(client):
    assert client.post("/api/approve", json={"id": "nope", "ok": True}).status_code == 404


def test_stats_range(client, tmp_path, monkeypatch):
    import json
    from food_console import stats
    from food_console import store
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "food.db")
    assert client.get("/api/stats").json()["metrics"] is None  # nothing saved yet
    orders = [
        {"provider": "swiggy", "service": "food", "order_id": "1", "placed_at": "2026-11-20T08:00:00Z", "status": "DELIVERED",
         "merchant": "A", "total_inr": 300, "items": []},
        {"provider": "zomato", "service": "food", "order_id": "2", "placed_at": "2026-10-10T08:00:00Z", "status": "DELIVERED",
         "merchant": "B", "total_inr": 500, "items": []},
    ]
    stats.save_orders({"orders": orders, "notes": "", "fetched_at": "x"})
    all_time = client.get("/api/stats").json()
    assert all_time["metrics"]["total"]["orders"] == 2 and all_time["previous"] is None
    nov = client.get("/api/stats?from=2026-11-01&to=2026-11-30").json()
    assert nov["metrics"]["total"]["spend"] == 300 and nov["all_count"] == 2
    assert nov["previous"]["spend"] == 500  # Oct 2 - Oct 31
    assert client.get("/api/stats?from=nope").status_code == 400


def test_stats_platform_and_compare(client, tmp_path, monkeypatch):
    import json
    from food_console import stats
    from food_console import store
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "food.db")
    orders = [
        {"provider": "swiggy", "service": "instamart", "order_id": "1", "placed_at": "2026-11-20T08:00:00Z",
         "status": "DELIVERED", "merchant": "Instamart", "total_inr": 300, "items": []},
        {"provider": "zomato", "service": "food", "order_id": "2", "placed_at": "2026-11-10T08:00:00Z",
         "status": "DELIVERED", "merchant": "B", "total_inr": 500, "items": []},
    ]
    stats.save_orders({"orders": orders, "notes": "", "fetched_at": "x"})
    z = client.get("/api/stats?platform=zomato").json()
    assert z["metrics"]["total"]["spend"] == 500 and z["platform_label"] == "Zomato"
    assert client.get("/api/stats?platform=nope").status_code == 400
    cmp = client.get("/api/stats/compare?by=service&from=2026-11-01&to=2026-11-30").json()
    assert [s["key"] for s in cmp["segments"]] == ["zomato", "swiggy:instamart"]
    assert len(cmp["orders"]) == 2
    assert client.get("/api/stats/compare?by=nope").status_code == 400


def test_pick_validates_input(client):
    r = client.get("/api/pick")
    assert r.status_code == 200 and "lean" in r.json()["goals"] and r.json()["last"] is None
    assert client.post("/api/pick", json={"goal": "nope"}).status_code == 400
    assert client.post("/api/pick", json={"platform": "dineout"}).status_code == 400
    assert client.post("/api/pick", json={"max_kcal": -1}).status_code == 400


def test_pick_runs_read_only_and_saves_result(client, tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    import food_console.web as web
    from food_console.store import Store

    state = client.state
    state.store = Store(tmp_path / "food.db", key=Fernet.generate_key())
    seen = {}

    async def fake_pick(agent, goal, **kw):
        seen["tools"] = sorted(t["name"] for t in agent._tools())
        seen["allow_orders"], seen["kw"] = agent.allow_orders, kw
        return {"dishes": [], "fits": 0, "listed_count": 0, "address_label": "Home", "notes": "", "goal": goal}

    monkeypatch.setattr(web, "pick_dishes", fake_pick)
    r = client.post("/api/pick", json={"goal": "protein", "platform": "zomato", "query": "chicken"})
    lines = [json.loads(line) for line in r.text.splitlines() if line.strip()]
    assert lines[-1]["type"] == "done" and lines[-1]["goal"]["min_protein"] == 40
    # Only read-only search tools of the requested platform: no order history, no place_order,
    # nothing from the other server.
    assert len(seen["tools"]) == 1 and seen["tools"][0].endswith("search_menu") and "zomato" in seen["tools"][0]
    assert seen["allow_orders"] is False and seen["kw"]["query"] == "chicken" and seen["kw"]["platforms"] == "Zomato"
    assert client.get("/api/pick").json()["last"]["platform"] == "zomato"
