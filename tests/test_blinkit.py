from conftest import FAKE_READ_ONLY
"""Blinkit: parsers, encrypted session, tool labels and launch through the bridge (no browser)."""
import asyncio
import os
import stat

from food_console.blinkit.parse import (
    inr,
    parse_card_text,
    parse_history_detail,
    parse_history_summary,
    split_qty,
    valid_product_id,
)


def test_card_text_as_served_by_blinkit():
    # Real card text (Nov 2026), one line per visual element.
    card = parse_card_text("17%\nOFF\n19 MINS\nMilky Mist Pro+ 25 g High Protein Milk\n250 ml\n₹82\n₹99\nADD")
    assert card == {"name": "Milky Mist Pro+ 25 g High Protein Milk", "variant": "250 ml", "price_inr": 82.0,
                    "mrp_inr": 99.0, "discount_pct": 17, "eta_min": 19, "in_stock": True}
    plain = parse_card_text("19 MINS\nAmul Taaza Toned Milk\n500 ml\n₹31\nADD")
    assert plain["price_inr"] == 31 and plain["mrp_inr"] is None and plain["discount_pct"] is None
    assert parse_card_text("Bread\n400 g\n₹1,050\nOut of Stock")["in_stock"] is False
    assert inr("Total ₹1,234.50") == 1234.5 and inr("free") is None


def test_history_parsers():
    s = parse_history_summary("Arrived in 9 minutes\n₹412\n12 Nov, 7:45 pm")
    assert s == {"status": "Arrived in 9 minutes", "total": "₹412", "date": "12 Nov, 7:45 pm"}
    page = ("Order summary\n3 items in this order\nAmul Taaza\nToned Milk\n500 ml x 2\n₹62\n"
            "Eggs\n6 pcs x 1\n₹54\nBill details\nItem total\n₹116\nBill total\n₹130")
    d = parse_history_detail(page)
    assert d["bill_total"] == "₹130"
    assert d["items"] == [
        {"name": "Amul Taaza Toned Milk", "variant": "500 ml", "quantity": 2, "price": "₹62"},
        {"name": "Eggs", "variant": "6 pcs", "quantity": 1, "price": "₹54"},
    ]
    assert parse_history_detail("no marker here") == {"items": [], "bill_total": ""}
    assert split_qty("2 x 750 ml x 1") == ("2 x 750 ml", 1)


def test_product_ids_are_numeric_only():
    assert valid_product_id("561268")
    for bad in ("", "abc", "1'] , div[id='2", "12 34", "1" * 16):
        assert not valid_product_id(bad)


def test_session_is_encrypted_and_owner_only(tmp_path):
    from food_console.blinkit import session

    path = tmp_path / "blinkit_session.enc"
    state = {"cookies": [{"name": "auth", "value": "secret-token-123"}], "origins": []}
    session.save_state(state, path)
    assert b"secret-token-123" not in path.read_bytes()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert session.load_state(path) == state
    assert not session.has_login(state)  # cookies alone (e.g. a saved location) are not a login
    logged_in = {"origins": [{"origin": "https://blinkit.com", "localStorage": [
        {"name": "auth", "value": '{"accessToken": "x", "phoneNumber": "98xxxxxx01"}'}]}]}
    assert session.has_login(logged_in)
    path.write_bytes(b"garbage")
    assert session.load_state(path) is None  # damaged/foreign file: logged out, no crash
    assert session.clear(path) and not path.exists()


def test_tool_labels():
    from food_console.blinkit.server import app

    tools = {t.name: t.annotations for t in asyncio.run(app.list_tools())}
    read_only = {n for n, a in tools.items() if a.read_only_hint}
    assert read_only == {"status", "list_addresses", "search", "view_cart", "order_history"}
    assert tools["place_order"].destructive_hint and tools["remove_from_cart"].destructive_hint
    assert not tools["checkout"].read_only_hint and not tools["send_otp"].read_only_hint


def test_server_starts_through_the_bridge_without_a_browser():
    from food_console.agent import OrderAgent
    from food_console.mcp_bridge import MCPBridge, load_server_params

    async def run():
        async with MCPBridge([("blinkit", load_server_params("blinkit-mcp"))], connect_timeout=60, read_only_tools=FAKE_READ_ONLY) as mcp:
            agent = OrderAgent.__new__(OrderAgent)
            asks = {t.mcp_name for t in mcp.tools.values() if agent.needs_approval(t)}
            assert {"place_order", "checkout", "add_to_cart", "send_otp", "verify_otp", "logout"} <= asks
            assert "search" not in asks and "order_history" not in asks

    asyncio.run(run())


def test_auth_knows_blinkit_is_a_local_session():
    from food_console import auth

    assert auth.APPS["blinkit"] == ["blinkit-mcp"] and "blinkit-mcp" in auth.LOCAL_SESSIONS


def test_cart_or_address_changes_void_the_checkout_total():
    """Regression: a total confirmed at checkout must not survive a later cart/address change."""
    from food_console.blinkit.browser import Blinkit

    bk = Blinkit()  # no browser: each call fails on the missing page, after voiding the total
    for call in (lambda: bk.change_quantity("610197", 1), lambda: bk.select_address(0), lambda: bk.set_location("x")):
        bk.checkout_total = 222.0
        try:
            asyncio.run(call())
        except Exception:
            pass
        assert bk.checkout_total is None

    bk.checkout_total = None
    bk.page = type("P", (), {"query_selector": lambda self, s: _async(object())})()
    r = asyncio.run(bk.place_order("cod", 222))
    assert r["ok"] is False and "does not match" in r["message"]


async def _async(value):
    return value
