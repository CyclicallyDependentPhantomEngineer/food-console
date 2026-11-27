"""SQLite store and the bridge's cache behaviour (fresh hits, 429 fallback, offline tools, invalidation)."""
import asyncio
import json
import os
import stat
import sys
import time
from pathlib import Path

from mcp import StdioServerParameters

from food_console.mcp_bridge import MCPBridge
from food_console.store import Store

FAKE = str(Path(__file__).with_name("fake_server.py"))


def test_store_roundtrip_and_permissions(tmp_path):
    s = Store(tmp_path / "food.db")
    assert stat.S_IMODE(os.stat(tmp_path / "food.db").st_mode) == 0o600
    s.put_result("food", "get_cart", {"b": 1, "a": 2}, "cart!")
    assert s.get_result("food", "get_cart", {"a": 2, "b": 1}).text == "cart!"  # arg order doesn't matter
    assert s.get_result("food", "get_cart", {"a": 3}) is None
    assert s.invalidate("food") == 1 and s.get_result("food", "get_cart", {"a": 2, "b": 1}) is None
    s.save_orders({"orders": [{"provider": "swiggy", "service": "food", "order_id": "1", "placed_at": "2026-11-01T00:00:00Z"}],
                   "fetched_at": "t", "notes": "n"})
    d = s.load_orders()
    assert d["notes"] == "n" and [o["order_id"] for o in d["orders"]] == ["1"]
    assert s.info()["orders"] == {"swiggy:food": 1}
    assert s.clear("orders") >= 1 and s.load_orders() is None


def test_legacy_json_imported_once(tmp_path):
    (tmp_path / "orders.json").write_text(json.dumps({"orders": [{"provider": "zomato", "service": "food", "order_id": "9"}],
                                                      "fetched_at": "t"}))
    s = Store(tmp_path / "food.db")
    assert [o["order_id"] for o in s.load_orders()["orders"]] == ["9"]
    assert not (tmp_path / "orders.json").exists()  # plaintext copy removed once stored encrypted


def _flaky_server(tmp_path, mode_file):
    """Fake MCP server whose behaviour is controlled by a file: 'ok', '429' (tool error) or 'down' (won't start)."""
    code = (
        "import sys, runpy, pathlib\n"
        f"mode = pathlib.Path({str(mode_file)!r}).read_text().strip()\n"
        "if mode == 'down':\n    sys.exit(1)\n"
        "if mode == '429':\n"
        "    import zomato_fake_429\n"
        f"runpy.run_path({FAKE!r}, run_name='__main__')\n"
    )
    return StdioServerParameters(command=sys.executable, args=["-c", code])


def test_cache_fresh_hit_fallback_and_invalidation(tmp_path):
    db = Store(tmp_path / "food.db")
    params = StdioServerParameters(command=sys.executable, args=[FAKE])

    async def run():
        async with MCPBridge([("food", params), ("zomato", params)], store=db, cache_ttl=60) as mcp:
            first = await mcp.call_ex("food_get_order_history", {"limit": 1})
            assert first.source == "live" and "Biryani House" in first.text
            again = await mcp.call_ex("food_get_order_history", {"limit": 1})
            assert again.source == "cache" and again.text == first.text  # no request within ttl

            # Server starts answering with a rate-limit error: stale cache is served, clearly marked.
            mcp.cache_ttl = 0
            async def rate_limited(*a, **k):
                from mcp import types
                return types.CallToolResult(content=[types.TextContent(type="text", text="HTTP 429 Too Many Requests")], is_error=True)
            real = mcp.sessions["food"].call_tool
            mcp.sessions["food"].call_tool = rate_limited
            fb = await mcp.call_ex("food_get_order_history", {"limit": 1})
            assert fb.source == "cache-fallback" and not fb.is_error
            assert fb.text.startswith("[Cached result from") and "rate-limiting" in fb.text and "Biryani House" in fb.text
            # Without a cached copy the rate-limit error is passed through.
            miss = await mcp.call_ex("food_get_order_history", {"limit": 2})
            assert miss.is_error and "429" in miss.text
            mcp.sessions["food"].call_tool = real

            # A successful account-changing call clears that server's cache only.
            await mcp.call_ex("zomato_get_order_history", {})
            assert (await mcp.call_ex("food_place_order", {"cart_id": "c"})).text == "placed c"
            assert db.get_result("food", "get_order_history", {"limit": 1}) is None
            assert db.get_result("zomato", "get_order_history", {}) is not None

    asyncio.run(run())


def test_offline_server_tools_served_from_cache(tmp_path):
    db = Store(tmp_path / "food.db")
    good = StdioServerParameters(command=sys.executable, args=[FAKE])
    down = StdioServerParameters(command=sys.executable, args=["-c", "import sys; sys.exit(1)"])

    async def first_run():  # both servers up: tool lists and a result get cached
        async with MCPBridge([("food", good), ("zomato", good)], store=db) as mcp:
            await mcp.call_ex("food_get_order_history", {})

    async def second_run():  # food is down (e.g. rate-limited at startup)
        async with MCPBridge([("food", down), ("zomato", good)], store=db, log_dir=tmp_path / "logs") as mcp:
            assert "food" in mcp.failed
            assert mcp.tools["food_get_order_history"].offline          # read-only tool kept from cache
            assert "food_place_order" not in mcp.tools                   # account-changing tool hidden
            r = await mcp.call_ex("food_get_order_history", {})
            assert r.source == "cache-fallback" and "is offline" in r.text and "Biryani House" in r.text
            miss = await mcp.call_ex("food_get_order_history", {"limit": 7})
            assert miss.is_error and "no cached result" in miss.text

    asyncio.run(first_run())
    asyncio.run(second_run())


def test_personal_data_is_encrypted_at_rest(tmp_path):
    s = Store(tmp_path / "food.db")
    s.put_result("zomato", "get_saved_addresses_for_user", {"address_id": "1077488435"}, "Flat 3C, Ambika Nath, Kolkata")
    s.save_orders({"orders": [{"provider": "zomato", "service": "food", "order_id": "8882123180",
                               "merchant": "Chai Point", "placed_at": "2026-11-01T00:00:00Z"}], "notes": "private note", "fetched_at": "t"})
    s.close()
    raw = b"".join(p.read_bytes() for p in tmp_path.glob("food.db*") if not p.name.endswith(".key"))
    for secret in [b"Ambika Nath", b"1077488435", b"Chai Point", b"8882123180", b"private note"]:
        assert secret not in raw, secret
    s2 = Store(tmp_path / "food.db")  # same key (from the key file) reads it back
    assert s2.get_result("zomato", "get_saved_addresses_for_user", {"address_id": "1077488435"}).text.startswith("Flat 3C")
    assert s2.load_orders()["orders"][0]["merchant"] == "Chai Point"
    s2.close()
    # With a different key the data is unreadable and treated as missing, not as garbage.
    from cryptography.fernet import Fernet
    s3 = Store(tmp_path / "food.db", key=Fernet.generate_key())
    assert s3.get_result("zomato", "get_saved_addresses_for_user", {"address_id": "1077488435"}) is None
    assert s3.load_orders() is None


def test_plaintext_v1_database_is_upgraded(tmp_path):
    import sqlite3
    db = sqlite3.connect(tmp_path / "food.db")
    db.executescript("""
        CREATE TABLE tool_results (server TEXT, tool TEXT, args_hash TEXT, args_json TEXT, result TEXT, fetched_at REAL,
                                   PRIMARY KEY (server, tool, args_hash));
        CREATE TABLE orders (provider TEXT, service TEXT, order_id TEXT, placed_at TEXT, data TEXT, saved_at REAL,
                             PRIMARY KEY (provider, service, order_id));
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    """)
    db.execute("INSERT INTO tool_results VALUES ('zomato','get_order_history','x','{}','SECRET-RESULT',1)")
    db.execute("""INSERT INTO orders VALUES ('swiggy','food','42','', '{"provider":"swiggy","service":"food","order_id":"42","merchant":"SECRET-PLACE"}', 1)""")
    db.execute("""INSERT INTO meta VALUES ('fetched_at', '"t"')""")
    db.commit(); db.close()
    s = Store(tmp_path / "food.db")
    assert s.get_result("zomato", "get_order_history", {}).text == "SECRET-RESULT"
    assert s.load_orders()["orders"][0]["merchant"] == "SECRET-PLACE"
    s.close()
    raw = b"".join(p.read_bytes() for p in tmp_path.glob("food.db*") if not p.name.endswith(".key"))
    assert b"SECRET-RESULT" not in raw and b"SECRET-PLACE" not in raw


def test_status_tools_are_never_fresh_cache_hits(tmp_path):
    db = Store(tmp_path / "food.db")
    params = StdioServerParameters(command=sys.executable, args=[FAKE])

    async def run():
        async with MCPBridge([("food", params)], store=db, cache_ttl=600) as mcp:
            first = await mcp.call_ex("payment_status", {})
            second = await mcp.call_ex("payment_status", {})
            # Polling sees the real change instead of a cached "pending".
            assert (first.text, first.source) == ("pending", "live")
            assert (second.text, second.source) == ("paid", "live")

    asyncio.run(run())


def test_district_history_goes_through_search_and_execute():
    from types import SimpleNamespace

    from food_console.stats import history_tool_filter

    t = lambda name, server="district", ro=True: SimpleNamespace(mcp_name=name, server=server, read_only=ro)  # noqa: E731
    assert history_tool_filter(t("search")) and history_tool_filter(t("execute"))
    assert not history_tool_filter(t("act", ro=False)) and not history_tool_filter(t("pay", ro=False))
    assert not history_tool_filter(t("search", server="zomato"))  # dispatch exception is District-only
