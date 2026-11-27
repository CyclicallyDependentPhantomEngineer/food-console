from conftest import FAKE_READ_ONLY, PAYMENT_POLICY
"""Payment items: UPI links -> QR, images, payment links; only from account-changing tools."""
import asyncio
import sys
from pathlib import Path

from mcp import StdioServerParameters

from food_console.mcp_bridge import MCPBridge
from food_console.payments import extract_from_text, for_model, upi_item

PAY = StdioServerParameters(command=sys.executable, args=[str(Path(__file__).with_name("fake_pay_server.py"))])


def test_upi_links_are_normalised_and_described():
    item = upi_item("tez://upi/pay?pa=merchant@okaxis&pn=Food%20Co&am=412.50&cu=INR&tn=Order%2042")
    assert item["uri"].startswith("upi://pay?pa=merchant@okaxis") and "am=412.50" in item["uri"]
    assert (item["payee_vpa"], item["payee_name"], item["amount_inr"], item["note"]) == \
        ("merchant@okaxis", "Food Co", 412.5, "Order 42")
    assert item["qr"].startswith("data:image/svg+xml;base64,")
    assert upi_item("upi://pay?am=10") is None            # no payee: not a payment request
    assert upi_item("upi://pay?pa=notavpa&am=10") is None


def test_links_images_and_unknown_hosts():
    png = "data:image/png;base64," + "iVBORw0KGgo" * 20
    text = ("Pay at https://rzp.io/i/abc123 or https://evil.example/pay/1 and "
            "https://www.swiggy.com/images/banner.png; qr: " + png)
    items = extract_from_text(text)
    kinds = [(i["kind"], i.get("host") or "") for i in items]
    assert ("link", "rzp.io") in kinds and ("image", "") in kinds
    assert not any(i.get("host") == "evil.example" for i in items)       # unknown host: never a QR or button
    assert not any("banner.png" in i.get("url", "") for i in items)       # known host, but not a payment URL
    model_text = for_model(text, items)
    assert "iVBORw0KGgo" not in model_text and "[QR image shown to the user]" in model_text
    assert "payment item(s)" in model_text


def test_bridge_shows_payments_only_for_account_changing_tools_and_honours_blocked_tools():
    async def run():
        async with MCPBridge([("pay", PAY)], blocked_tools={"pay": {"wallet_pay"}}, read_only_tools=FAKE_READ_ONLY, payment_tools=PAYMENT_POLICY) as mcp:
            assert "wallet_pay" not in mcp.tools                            # blocked: not offered at all
            order = await mcp.call_ex("place_order", {"amount": 412.5, "order_id": "order-42", "payee": "merchant@okaxis"})
            kinds = sorted(i["kind"] for i in order.payments)
            assert kinds == ["upi"]
            upi = next(i for i in order.payments if i["kind"] == "upi")
            assert upi["payee_vpa"] == "merchant@okaxis" and upi["amount_inr"] == 412.5
            assert "payment item(s)" in order.text
            listing = await mcp.call_ex("search", {"query": "biryani"})   # read-only: never a payment card
            assert listing.payments == [] and "attacker@ybl" in listing.text

    asyncio.run(run())


def test_agent_emits_payment_event():
    from types import SimpleNamespace

    from food_console.agent import OrderAgent

    async def run():
        async with MCPBridge([("pay", PAY)], read_only_tools=FAKE_READ_ONLY, payment_tools=PAYMENT_POLICY) as mcp:
            events = []

            async def approve(tool, args):
                return True

            agent = OrderAgent(mcp, approve, on_event=events.append)
            await agent._run_tool(SimpleNamespace(id="t1", name="place_order", input={"amount": 99, "order_id": "order-42", "payee": "merchant@okaxis"}))
            pay = [e for e in events if e["type"] == "payment"]
            assert len(pay) == 1 and pay[0]["tool"] == "pay:place_order" and pay[0]["items"]

    asyncio.run(run())


def test_agent_refuses_calls_the_arg_guard_rejects():
    from types import SimpleNamespace

    from food_console.agent import OrderAgent

    async def run():
        async with MCPBridge([("pay", PAY)], read_only_tools=FAKE_READ_ONLY, payment_tools=PAYMENT_POLICY) as mcp:
            events = []
            agent = OrderAgent(mcp, approve=None, on_event=events.append)
            agent.arg_guard = lambda tool, args: "Refused: wrong keyword" if args.get("query") != "biryani" else None
            bad = await agent._run_tool(SimpleNamespace(id="t1", name="search", input={"query": "pizza"}))
            ok = await agent._run_tool(SimpleNamespace(id="t2", name="search", input={"query": "biryani"}))
            assert bad["is_error"] and bad["content"] == "Refused: wrong keyword"
            assert not ok.get("is_error") and any(e["type"] == "tool_declined" for e in events)

    asyncio.run(run())
