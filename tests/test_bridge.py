"""Smoke tests: bridge + approval gate against fake MCP servers (no Claude API call)."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

from mcp import StdioServerParameters

from food_console.agent import OrderAgent
from food_console.mcp_bridge import PROVIDERS, MCPBridge

FAKE = StdioServerParameters(command=sys.executable, args=[str(Path(__file__).with_name("fake_server.py"))])
BROKEN = StdioServerParameters(command=sys.executable, args=["-c", "import sys; sys.exit(1)"])


def test_bridge_and_gate():
    async def run():
        asked = []

        async def deny(tool, args):
            asked.append(tool.mcp_name)
            return False

        async with MCPBridge([("zomato", FAKE)]) as mcp:
            names = [t["name"] for t in mcp.claude_tools()]
            assert names == ["get_order_history", "payment_status", "place_order", "search_menu"]  # single server: no prefix
            agent = OrderAgent(mcp, deny)
            assert not agent.needs_approval(mcp.tools["get_order_history"])
            assert agent.needs_approval(mcp.tools["place_order"])

            ok = await agent._run_tool(SimpleNamespace(id="t1", name="get_order_history", input={"limit": 1}))
            assert not ok["is_error"] and "Biryani House" in ok["content"]

            denied = await agent._run_tool(SimpleNamespace(id="t2", name="place_order", input={"cart_id": "c1"}))
            assert denied["is_error"] and "declined" in denied["content"]
            assert asked == ["place_order"]

            agent.allow_orders = False
            blocked = await agent._run_tool(SimpleNamespace(id="t3", name="place_order", input={"cart_id": "c1"}))
            assert blocked["is_error"] and "disabled" in blocked["content"]

    asyncio.run(run())


def test_multi_server_prefixes_and_routes():
    async def run():
        async def allow(tool, args):
            return True

        async with MCPBridge([("food", FAKE), ("instamart", FAKE), ("dineout", BROKEN)]) as mcp:
            assert set(mcp.sessions) == {"food", "instamart"}  # broken server skipped, not fatal
            assert sorted(mcp.tools) == [
                "food_get_order_history",
                "food_payment_status",
                "food_place_order",
                "food_search_menu",
                "instamart_get_order_history",
                "instamart_payment_status",
                "instamart_place_order",
                "instamart_search_menu",
            ]
            agent = OrderAgent(mcp, allow, provider=PROVIDERS["swiggy"])
            assert "Swiggy" in agent.system and "Instamart" in agent.system
            res = await agent._run_tool(SimpleNamespace(id="t1", name="instamart_place_order", input={"cart_id": "x"}))
            assert res["content"] == "placed x" and not res["is_error"]

    asyncio.run(run())


def test_read_only_only_from_annotations_or_allowlist():
    from mcp import types
    from food_console.mcp_bridge import BridgedTool, load_read_only_tools

    allow = load_read_only_tools("zomato-mcp")  # the real config's allowlist

    def bt(name, ann=None):
        t = types.Tool(name=name, input_schema={"type": "object"}, annotations=ann)
        return BridgedTool("zomato", name, name, t, None, name in allow)

    for n in ["get_order_history", "get_saved_addresses_for_user", "get_menu_items_listing",
              "get_order_tracking_info", "get_restaurant_menu_by_categories", "get_restaurants_for_keyword"]:
        assert bt(n).read_only and bt(n).read_only_configured, n
    # Not listed -> asks, however harmless the name looks (no name-based trust any more).
    for n in ["create_cart", "checkout_cart", "bind_user_number", "get_new_tool_zomato_added_later"]:
        assert not bt(n).read_only, n
    # A server that does annotate is trusted as-is; the allowlist doesn't override it.
    assert not BridgedTool("s", "get_cart", "get_cart", types.Tool(name="get_cart", input_schema={"type": "object"},
                           annotations=types.ToolAnnotations(read_only_hint=False)), None, True).read_only
    assert load_read_only_tools("swiggy-food") == set()


def test_rate_limit_reason_and_reconnect(tmp_path):
    """A server that is rate-limited at startup shows a readable reason and comes back via reconnect()."""
    flag = tmp_path / "second-try"
    flaky = StdioServerParameters(command=sys.executable, args=["-c", (
        "import os, runpy, sys\n"
        f"flag = {str(flag)!r}\n"
        "if not os.path.exists(flag):\n"
        "    open(flag, 'w').close()\n"
        "    print('Error POSTing to endpoint (HTTP 429): Too Many Requests', file=sys.stderr)\n"
        "    sys.exit(1)\n"
        f"runpy.run_path({str(Path(__file__).with_name('fake_server.py'))!r}, run_name='__main__')\n"
    )])

    async def run():
        async with MCPBridge([("zomato", FAKE), ("food", flaky)], log_dir=tmp_path / "logs") as mcp:
            assert set(mcp.sessions) == {"zomato"}
            assert "rate-limited" in mcp.failed["food"]
            assert "429" in (tmp_path / "logs" / "food.log").read_text()
            # Reconnect from a different task than the one that opened the bridge.
            results = await asyncio.create_task(mcp.reconnect())
            assert results == {"food": "ok"} and not mcp.failed
            assert "food_place_order" in mcp.tools
            text, err = await mcp.call("food_get_order_history", {})
            assert not err and "Biryani House" in text

    asyncio.run(run())  # also checks shutdown works after a cross-task reconnect


def test_no_server_allowed_when_not_required(tmp_path):
    broken = StdioServerParameters(command=sys.executable, args=["-c", "import sys; sys.exit(1)"])

    async def run():
        async with MCPBridge([("a", broken), ("b", broken)], log_dir=tmp_path, require_any=False) as mcp:
            assert not mcp.sessions and set(mcp.failed) == {"a", "b"} and mcp.tools == {}

    asyncio.run(run())


def test_every_non_read_only_tool_needs_approval():
    """Regression: the old name-based gate let these run without asking in the terminal chat."""
    from mcp import types
    from food_console.mcp_bridge import BridgedTool

    agent = OrderAgent.__new__(OrderAgent)

    def bt(name, ann=None):
        return BridgedTool("x", name, name, types.Tool(name=name, input_schema={"type": "object"}, annotations=ann), None)

    write = types.ToolAnnotations(read_only_hint=False)
    for tool in [bt("create_cart"), bt("bind_user_number"), bt("bind_user_number_verify_code"),
                 bt("update_food_cart", write), bt("create_address", write), bt("apply_food_coupon", write)]:
        assert agent.needs_approval(tool), tool.mcp_name
    assert agent.needs_approval(bt("get_order_history"))  # not annotated, not allowlisted here
    assert not agent.needs_approval(bt("get_addresses", types.ToolAnnotations(read_only_hint=True)))
    # Zepto labels its order tools "not destructive" with no read-only flag at all: still asks.
    no_ro_flag = types.ToolAnnotations(destructive_hint=False, open_world_hint=False)
    for name in ("create_order", "create_online_payment_order", "create_wallet_order", "select_saved_address"):
        assert agent.needs_approval(bt(name, no_ro_flag)), name


def test_remote_pids_matches_pinned_version():
    import subprocess
    from food_console.mcp_bridge import remote_pids
    url = "https://pinned.test/mcp"
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "mcp-remote@0.17.1", url])
    try:
        import time
        for _ in range(50):
            if p.pid in remote_pids(url):
                break
            time.sleep(0.1)
        assert p.pid in remote_pids(url)
    finally:
        p.kill()


def test_log_and_cache_dirs_are_owner_only(tmp_path):
    import stat
    from food_console.store import Store
    Store(tmp_path / "c" / "food.db").close()
    assert stat.S_IMODE((tmp_path / "c").stat().st_mode) == 0o700
    broken = StdioServerParameters(command=sys.executable, args=["-c", "import sys; sys.exit(1)"])

    async def run():
        async with MCPBridge([("a", broken), ("b", broken)], log_dir=tmp_path / "logs", require_any=False):
            pass

    asyncio.run(run())
    assert stat.S_IMODE((tmp_path / "logs").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "logs" / "a.log").stat().st_mode) == 0o600
