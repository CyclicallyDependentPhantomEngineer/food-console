"""Blinkit MCP server (stdio only). Run with `blinkit-mcp` or `python -m food_console.blinkit.server`.

Tool labels drive the approval gate in food-console (and in other MCP clients that honour
them): only tools marked read-only run without asking.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .browser import Blinkit, BlinkitError

app = MCPServer(
    "blinkit",
    instructions=(
        "Unofficial Blinkit (grocery delivery) access through a Chrome window on the user's machine. "
        "Flow: status -> (send_otp/verify_otp if logged out) -> set delivery location (list_addresses + "
        "select_address, or set_location) -> search -> add_to_cart -> view_cart -> show the user the items "
        "and total and get their OK -> checkout(expected_total_inr) -> ask cod or upi_qr -> "
        "place_order(payment, expected_total_inr). Never pick a payment method for the user."
    ),
)
_bk = Blinkit()

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)


async def _run(fn, *args) -> str:
    async with _bk.lock:  # one browser page: one action at a time
        try:
            await _bk.start()
            result: Any = await fn(*args)
        except BlinkitError as exc:
            result = {"ok": False, "error": str(exc)}
        except Exception as exc:  # report page/timeouts to the model instead of crashing the server
            result = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)


@app.tool(annotations=READ)
async def status() -> str:
    """Whether the user is logged in to Blinkit and the current delivery location."""
    return await _run(_bk.status)


@app.tool(annotations=WRITE)
async def send_otp(phone_number: str) -> str:
    """Start logging in: Blinkit sends an OTP by SMS to this 10-digit mobile number. Ask the user for the
    number first and pass exactly what they typed; never guess or reuse one."""
    return await _run(_bk.send_otp, phone_number)


@app.tool(annotations=WRITE)
async def verify_otp(otp: str) -> str:
    """Finish logging in with the OTP the user received. Pass exactly what the user typed; never repeat it back."""
    return await _run(_bk.verify_otp, otp)


@app.tool(annotations=DESTRUCTIVE)
async def logout() -> str:
    """Log out of Blinkit on this machine and delete the saved session."""
    return await _run(_bk.logout)


@app.tool(annotations=WRITE)
async def set_location(place: str) -> str:
    """Set the delivery location by searching a place name (area, landmark, or full address); picks the first
    match and returns it. Prefer list_addresses + select_address when the user is logged in."""
    return await _run(_bk.set_location, place)


@app.tool(annotations=READ)
async def list_addresses() -> str:
    """List the user's saved delivery addresses (index, label, details). Requires login."""
    return await _run(_bk.addresses)


@app.tool(annotations=WRITE)
async def select_address(index: int) -> str:
    """Deliver to the saved address at this index (from list_addresses)."""
    return await _run(_bk.select_address, index)


@app.tool(annotations=READ)
async def search(query: str, limit: int = 20) -> str:
    """Search Blinkit products for the current delivery location. Returns id, name, variant (pack size),
    price_inr, mrp_inr, discount_pct, eta_min, in_stock and sponsored for each result. Use one concrete
    product per call."""
    return await _run(_bk.search, query, max(1, min(int(limit), 40)))


@app.tool(annotations=WRITE)
async def add_to_cart(product_id: str, quantity: int = 1) -> str:
    """Add `quantity` units of a product (numeric id from search) to the cart. Returns the quantity before and after."""
    return await _run(_bk.change_quantity, product_id, max(1, min(int(quantity), 20)))


@app.tool(annotations=DESTRUCTIVE)
async def remove_from_cart(product_id: str, quantity: int = 1) -> str:
    """Remove `quantity` units of a product from the cart."""
    return await _run(_bk.change_quantity, product_id, -max(1, min(int(quantity), 20)))


@app.tool(annotations=READ)
async def view_cart() -> str:
    """Current cart: items (name, variant, price, quantity), bill lines, delivery address and total_inr.
    Always show this to the user and get their OK before checkout."""
    return await _run(_bk.cart)


@app.tool(annotations=WRITE)
async def checkout(expected_total_inr: float) -> str:
    """Go from the cart to the payment page. expected_total_inr must be the cart total the user approved;
    if the cart total differs, nothing happens and the current cart is returned."""
    return await _run(_bk.checkout, expected_total_inr)


@app.tool(annotations=DESTRUCTIVE)
async def place_order(payment: str, expected_total_inr: float) -> str:
    """Place the order. payment is 'cod' (cash on delivery: the order is placed immediately) or 'upi_qr'
    (shows a UPI QR on screen; the order is placed when the user pays it). Ask the user which one; never
    choose for them. expected_total_inr must equal the total confirmed at checkout."""
    return await _run(_bk.place_order, payment, expected_total_inr)


@app.tool(annotations=READ)
async def order_history(count: int = 10) -> str:
    """Recent Blinkit orders, newest first: order_id, date, status, total and items (name, variant, quantity,
    price). count: 1-30 (each order opens its detail page, so larger counts are slower)."""
    return await _run(_bk.order_history, count)


def main() -> None:
    app.run()


if __name__ == "__main__":
    main()
