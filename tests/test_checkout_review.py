"""Swiggy checkout contract fixtures from the official Builders Club references.

No live accounts/orders: these exercise the shared bridge, chat approval and HTTP path.
https://mcp.swiggy.com/builders/docs/reference/food/get_food_cart/
https://mcp.swiggy.com/builders/docs/reference/food/place_food_order/
"""
import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp import types

from test_web import client
from food_console.agent import OrderAgent
from food_console.mcp_bridge import BridgedTool, MCPBridge, load_read_only_tools
from food_console.payments import extract
from food_console.stats import history_tool_filter

ARGS = {"addressId": "home", "paymentMethod": "UPI", "generateUPIQR": True}
CART = {"addressId": "home", "data": {"cart_id": "cart-1", "restaurant": {"id": "r1", "name": "Lunch"},
        "items": [{"menu_item_id": "dish-1", "name": "Rice", "quantity": 1, "total": 99}],
        "pricing": {"item_total": 99, "delivery_charge": 0, "taxes_and_charges": 1, "to_pay": 100}}}
PAYMENT = {"status": "PENDING_PAYMENT", "paymentMethod": "UPI", "orderId": "order-1", "paasId": "payment-1",
           "addressId": "home", "cartId": "cart-1", "totalAmount": 100,
           "upiIntentUrl": "upi://pay?pa=merchant@bank&am=100&cu=INR",
           "product_name": "upi://pay?pa=attacker@bank&am=100"}


def result(data, envelope=False):
    if envelope:
        return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps({"success": True, "data": data}))])
    return types.CallToolResult(content=[], structured_content=data)


def checkout(bridge=None, envelope=False):
    bridge = bridge or MCPBridge([])
    cart = copy.deepcopy(CART)
    payment = copy.deepcopy(PAYMENT)
    async def call(name, args):
        return result(copy.deepcopy(cart if name == "get_food_cart" else payment), envelope)
    session = SimpleNamespace(call_tool=AsyncMock(side_effect=call))
    for name, ro in (("get_food_cart", True), ("place_food_order", False)):
        bridge.tools[name] = BridgedTool("food", name, name, types.Tool(name=name, input_schema={"type": "object"}), session, ro)
    bridge.payment_tools = {"food": {"place_food_order": {"adapter": "swiggy-food"}}}
    return bridge, bridge.tools["place_food_order"], session, cart, payment


@pytest.mark.parametrize("envelope", [False, True])
def test_live_cart_review_restores_qr_and_is_single_use(envelope):
    async def run():
        bridge, tool, session, _, _ = checkout(envelope=envelope)
        unreviewed = await bridge.call_ex(tool.claude_name, ARGS)
        assert unreviewed.is_error
        session.call_tool.assert_not_called()
        review = await bridge.prepare_review(tool, ARGS)
        assert review.cart["total_inr"] == "100.00"
        r = await bridge.call_ex(tool.claude_name, ARGS, expected_tool=tool, review=review)
        assert not r.is_error
        assert r.payments[0]["order_id"] == "order-1"
        assert r.payments[0]["amount_inr"] == 100
        assert r.payments[0]["payee_vpa"] == "merchant@bank"
        assert r.payments[0]["qr"].startswith("data:image/svg+xml;base64,")
        assert [c.args[0] for c in session.call_tool.call_args_list] == ["get_food_cart", "get_food_cart", "place_food_order"]
        assert (await bridge.call_ex(tool.claude_name, ARGS, review=review)).is_error
        assert session.call_tool.call_count == 3
    asyncio.run(run())


@pytest.mark.parametrize("change", ["price", "items", "address", "expired", "arguments", "offline", "tool"])
def test_changed_or_unverifiable_cart_never_submits_order(change):
    async def run():
        bridge, tool, session, cart, _ = checkout()
        review = await bridge.prepare_review(tool, ARGS)
        args = dict(ARGS)
        if change == "price": cart["data"]["pricing"]["to_pay"] = 101
        if change == "items": cart["data"]["items"][0]["menu_item_id"] = "another-dish-same-price"
        if change == "address": cart["addressId"] = "office"
        if change == "expired": review.expires = 0
        if change == "arguments": args["addressId"] = "office"
        if change == "offline": session.call_tool.side_effect = TimeoutError()
        if change == "tool": bridge.tools[tool.claude_name] = copy.copy(tool)
        r = await bridge.call_ex(tool.claude_name, args, expected_tool=tool, review=review)
        assert r.is_error
        assert all(c.args[0] == "get_food_cart" for c in session.call_tool.call_args_list)
    asyncio.run(run())


@pytest.mark.parametrize("field,value", [("cartId", "other"), ("addressId", "other"), ("totalAmount", 101),
    ("upiIntentUrl", "upi://pay?pa=merchant@bank&am=101"), ("upiIntentUrl", None), ("orderId", ""),
    ("status", "FAILED"), ("upiIntentUrl", "upi://pay?pa=merchant@bank&am=100&am=101")])
def test_bad_checkout_response_suppresses_qr_without_claiming_order_failed(field, value):
    async def run():
        bridge, tool, session, _, payment = checkout()
        review = await bridge.prepare_review(tool, ARGS)
        payment[field] = value
        r = await bridge.call_ex(tool.claude_name, ARGS, review=review)
        assert not r.is_error and r.payments == []
        assert "Do not repeat the action" in r.text
        assert sum(c.args[0] == "place_food_order" for c in session.call_tool.call_args_list) == 1
    asyncio.run(run())


def test_chat_displays_cart_before_approval_and_binds_it():
    async def run():
        bridge, tool, _, cart, _ = checkout()
        shown = []
        async def approve(t, args):
            shown.append(args)
            cart["data"]["pricing"]["to_pay"] = 999
            return True
        agent = OrderAgent(bridge, approve)
        r = await agent._run_tool(SimpleNamespace(name=tool.claude_name, input=ARGS, id="call"))
        assert shown[0]["arguments"] == ARGS
        assert shown[0]["reviewed_cart"]["total_inr"] == "100.00"
        assert r["is_error"] and "cart changed" in r["content"]
    asyncio.run(run())


def test_actions_review_shows_total_and_cannot_accept_a_forged_total(client):
    bridge, _, session, _, _ = checkout(client.state.mcp)
    url = "/api/tools/place_food_order"
    response = client.post(url, json={"args": ARGS})
    assert response.status_code == 409
    review = response.json()
    assert review["reviewed_cart"]["total_inr"] == "100.00"
    r = client.post(url, json={"args": ARGS, "confirm": True, "approval_id": review["approval_id"],
                               "reviewed_cart": {"total_inr": "999"}}).json()
    assert not r["is_error"] and r["payments"][0]["amount_inr"] == 100
    assert client.post(url, json={"args": ARGS, "confirm": True, "approval_id": review["approval_id"]}).status_code == 409


def test_district_documented_read_dispatch_is_allowlisted_and_mutations_still_need_approval():
    policy = load_read_only_tools("district-mcp")
    # execute's published contract is read-only; act/pay are separate endpoints.
    assert "execute" in policy
    bridge = MCPBridge([])
    agent = OrderAgent(bridge, AsyncMock(return_value=True), allow_orders=False)
    for name in ("search", "execute", "act", "pay", "cancel_payment", "new_tool"):
        t = BridgedTool("district", name, name, types.Tool(name=name, input_schema={}), None, name in policy)
        assert agent.needs_approval(t) == (name not in {"search", "execute"})
        if name in {"search", "execute"}:
            assert history_tool_filter(t)


def test_optional_cart_id_and_volatile_eta_do_not_disable_qr():
    async def run():
        bridge, tool, _, cart, payment = checkout()
        cart['data'].pop('cart_id')  # optional in the documented cart schema
        payment['cartId'] = None
        cart['data']['restaurant']['deliverySubtitle'] = '30 minutes'
        review = await bridge.prepare_review(tool, ARGS)
        cart['data']['restaurant']['deliverySubtitle'] = '31 minutes'
        cart['data']['pricing']['to_pay'] = 100.0
        r = await bridge.call_ex(tool.claude_name, ARGS, review=review)
        assert not r.is_error and r.payments[0]['amount_inr'] == 100
    asyncio.run(run())


@pytest.mark.parametrize('changed', ['expiry', 'tool'])
def test_review_is_checked_again_after_the_final_cart_request(changed):
    async def run():
        bridge, tool, session, _, _ = checkout()
        review = await bridge.prepare_review(tool, ARGS)
        async def fetch(name, args):
            assert name == 'get_food_cart'
            if changed == 'expiry':
                review.expires = 0
            else:
                bridge.tools[tool.claude_name] = copy.copy(tool)
            return result(CART)
        session.call_tool.side_effect = fetch
        r = await bridge.call_ex(tool.claude_name, ARGS, review=review)
        assert r.is_error
        assert all(c.args[0] == 'get_food_cart' for c in session.call_tool.call_args_list)
    asyncio.run(run())
