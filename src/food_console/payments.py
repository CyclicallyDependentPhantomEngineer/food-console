"""Reviewed, structured UPI payment adapters.

Only configured fields bound to the approved order, merchant and amount may produce a
payment card. Arbitrary text, images and opaque links from tools are not payment authority.
"""

from __future__ import annotations

import base64
import io
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

UPI_URI = re.compile(r"\b(?:upi|tez|gpay|phonepe|paytmmp|paytm|bhim|credpay)://[^\s\"'<>\\`]+", re.I)
DATA_IMAGE = re.compile(r"data:image/(png|jpeg|jpg|gif|webp);base64,([A-Za-z0-9+/=]{100,})")
HTTPS_URL = re.compile(r"https://[^\s\"'<>\\`)\]]+")
PAYMENT_NAME = re.compile(r"pay|order|checkout|qr|book", re.I)
# Links become a payment card only on these hosts; anything else stays plain text in the reply.
# Payment gateways: any link on them is a payment page.
GATEWAY_HOSTS = ("rzp.io", "razorpay.com", "razorpay.me", "paytm.com", "paytm.in", "phonepe.com", "juspay.in",
                 "cashfree.com", "payu.in", "billdesk.com")
# The services themselves: only links whose path looks like payment.
SERVICE_HOSTS = ("zepto.co.in", "zeptonow.com", "swiggy.com", "zomato.com", "district.in", "blinkit.com")
MAX_IMAGE_BYTES = 1_000_000
MAX_URI_CHARS = 2048
MAX_PAYMENT_ITEMS = 4


def money(value: Any) -> Decimal:
    """Positive, finite INR amounts with exact paise precision."""
    if isinstance(value, bool):
        raise ValueError("invalid amount")
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or not 0 < amount <= 1_000_000 or amount != amount.quantize(Decimal("0.01")):
            raise ValueError("invalid amount")
        return amount
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("invalid amount") from exc


def qr_data_uri(text: str) -> str:
    """SVG QR code as a data: URI (rendered in an <img>, so it can't run scripts)."""
    import segno

    if len(text) > MAX_URI_CHARS:
        raise ValueError("payment URI too long")

    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="svg", scale=6, border=2, dark="#111", light="#fff")
    return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode()


def upi_item(uri: str) -> dict[str, Any] | None:
    """Normalise a UPI URI and describe who gets paid how much. None if it has no payee."""
    if len(uri) > MAX_URI_CHARS:
        return None
    try:
        parts = urlsplit(uri.rstrip(".,;"))
    except ValueError:
        return None
    if parts.scheme.lower() not in {"upi", "tez", "gpay", "phonepe", "paytmmp", "paytm", "bhim", "credpay"}:
        return None
    query = parse_qs(parts.query, keep_blank_values=True)
    if any(len(values) != 1 for values in query.values()) or query.get("cu", ["INR"]) != ["INR"]:
        return None
    payee = (query.get("pa") or [""])[0].strip()
    if not payee or "@" not in payee:
        return None
    clean = "upi://pay?" + urlencode({k: v[0] for k, v in query.items()}, safe="@:/")
    try:
        amount = money((query.get("am") or [""])[0])
    except ValueError:
        return None
    return {
        "kind": "upi",
        "uri": clean,
        "payee_vpa": payee,
        "payee_name": (query.get("pn") or [""])[0],
        "amount_inr": float(amount),
        "note": (query.get("tn") or [""])[0],
        "qr": qr_data_uri(clean),
    }


def _strings(node: Any):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)


def swiggy_payload(result: Any) -> dict:
    """Swiggy's documented structuredContent / JSON-envelope response contract.

    https://mcp.swiggy.com/builders/docs/build/read-tool-results/
    Never search human summaries, product fields, or images for payment authority.
    """
    from mcp import types
    if result.is_error:
        raise ValueError("Swiggy could not verify the cart/payment")
    data = result.structured_content
    if data is None:
        if len(result.content) != 1 or not isinstance(result.content[0], types.TextContent):
            raise ValueError("missing structured Swiggy response")
        envelope = json.loads(result.content[0].text)
        if envelope.get("success") is not True:
            raise ValueError("unsuccessful Swiggy response")
        data = envelope["data"]
    if not isinstance(data, dict) or data.get("success") is False:
        raise ValueError("invalid Swiggy response")
    return data


def swiggy_cart(result: Any, arguments: dict) -> dict:
    """Canonical fields shown at review and rechecked immediately before checkout.

    https://mcp.swiggy.com/builders/docs/reference/food/get_food_cart/
    """
    data = swiggy_payload(result)
    cart = data["data"]
    if data.get("addressId") != arguments.get("addressId"):
        raise ValueError("cart address does not match the order")
    cart_id = cart.get("cart_id")
    items = cart.get("items")
    if ((cart_id is not None and (not isinstance(cart_id, str) or not cart_id))
            or not isinstance(items, list) or not items
            or any(not isinstance(item, dict) or not item.get("menu_item_id") or not item.get("quantity") for item in items)):
        raise ValueError("cart cannot be verified")
    selected = [{k: item[k] for k in ("menu_item_id", "name", "quantity", "subtotal", "total",
                                     "final_price", "variants", "addons") if k in item} for item in items]
    return {"cart_id": cart_id, "address_id": data["addressId"],
            "total_inr": str(money(cart["pricing"]["to_pay"]).quantize(Decimal("0.01"))),
            "restaurant": {k: v for k, v in (cart.get("restaurant") or {}).items() if k in ("id", "name", "area")},
            "items": selected}


def swiggy_payment(result: Any, arguments: dict, reviewed_cart: dict | None) -> list[dict[str, Any]]:
    """The dedicated checkout URI must match the approved cart/address and total.

    https://mcp.swiggy.com/builders/docs/reference/food/place_food_order/
    """
    if arguments.get("paymentMethod") != "UPI":
        return []
    if not reviewed_cart:
        raise ValueError("missing approved cart")
    data = swiggy_payload(result)
    if (data.get("status") != "PENDING_PAYMENT" or data.get("paymentMethod") != "UPI"
            or (data.get("cartId") is not None and reviewed_cart["cart_id"] is not None
                and data["cartId"] != reviewed_cart["cart_id"])
            or data.get("addressId") != reviewed_cart["address_id"]):
        raise ValueError("payment does not belong to the approved cart")
    order = data.get("orderId")
    if not isinstance(order, str) or not order or not data.get("paasId"):
        raise ValueError("missing payment order identity")
    uri = data.get("upiIntentUrl")
    if not isinstance(uri, str) or urlsplit(uri).netloc != "pay":
        raise ValueError("invalid checkout UPI URI")
    item = upi_item(uri)
    amount = money(reviewed_cart["total_inr"])
    if item is None or money(item["amount_inr"]) != amount:
        raise ValueError("QR amount differs from the approved total")
    if data.get("totalAmount") is not None and money(data["totalAmount"]) != amount:
        raise ValueError("checkout total differs from the approved total")
    return [{**item, "order_id": order}]


def extract(result: Any, *, policy: dict[str, Any] | None = None,
            arguments: dict[str, Any] | None = None, reviewed_cart: dict | None = None) -> list[dict[str, Any]]:
    """Only a reviewed structured field bound to the approved order/payee/amount.

    A locally selected adapter supplies the binding contract. Opaque links and images
    cannot establish those bindings and stay in the provider app instead.
    """
    from mcp import types
    if not policy:
        return []
    args = arguments or {}
    if policy.get("adapter") == "swiggy-food":
        return swiggy_payment(result, args, reviewed_cart)
    data = getattr(result, "structured_content", None)
    if data is None:
        blocks = getattr(result, "content", [])
        if len(blocks) != 1 or not isinstance(blocks[0], types.TextContent):
            raise ValueError("expected a structured payment response")
        data = json.loads(blocks[0].text)

    def field(path):
        if not isinstance(path, list) or not path or not all(isinstance(k, str) for k in path):
            raise ValueError("invalid payment field path")
        value = data
        for key in path:
            value = value[key]
        return value

    order = args[policy["orderArgument"]]
    if not isinstance(order, str) or not order or field(policy["orderField"]) != order:
        raise ValueError("payment order mismatch")
    uri = field(policy["uriField"])
    if not isinstance(uri, str):
        raise ValueError("invalid payment URI")
    item = upi_item(uri)
    if (item is None or item["payee_vpa"] != args[policy["payeeArgument"]]
            or money(item["amount_inr"]) != money(args[policy["amountArgument"]])):
        raise ValueError("payment payee or amount mismatch")
    return [{**item, "order_id": order}]


def extract_from_text(text: str, items: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    # Legacy parser utility only; never used to authorize/render live tool results.
    text = text[:64_000]
    items = list(items or [])
    seen = {i.get("uri") or i.get("url") or i.get("src") for i in items}
    for m in UPI_URI.finditer(text):
        item = upi_item(m.group(0))
        if item and item["uri"] not in seen:
            seen.add(item["uri"])
            items.append(item)
    for m in DATA_IMAGE.finditer(text):
        raw = m.group(2)
        src = f"data:image/{m.group(1)};base64,{raw}"
        if src not in seen and len(raw) * 3 // 4 <= MAX_IMAGE_BYTES:
            seen.add(src)
            items.append({"kind": "image", "src": src})
    for m in HTTPS_URL.finditer(DATA_IMAGE.sub("", text)):
        url = m.group(0).rstrip(".,;")
        try:
            host = (urlsplit(url).hostname or "").lower()
        except ValueError:
            continue
        if len(url) > MAX_URI_CHARS:
            continue
        on = lambda hosts: any(host == h or host.endswith("." + h) for h in hosts)  # noqa: E731
        if url in seen:
            continue
        if not on(GATEWAY_HOSTS) and not (on(SERVICE_HOSTS) and re.search(r"pay|checkout|txn|upi", urlsplit(url).path, re.I)):
            continue  # unknown host, or e.g. an image / tracking URL on a service's own site
        seen.add(url)
        items.append({"kind": "link", "url": url, "host": host, "qr": qr_data_uri(url)})
    return items[:MAX_PAYMENT_ITEMS]


def for_model(text: str, items: list[dict[str, Any]]) -> str:
    """Tool text for Claude: inline images replaced by a marker, plus what the user was shown."""
    text = DATA_IMAGE.sub("[QR image shown to the user]", text)
    if not items:
        return text
    kinds = sorted({i["kind"] for i in items})
    return (text + f"\n\n[The app showed the user {len(items)} payment item(s) ({', '.join(kinds)}): "
            "a UPI QR / payment link with the payee and amount. Tell the user to scan or open it, check "
            "the payee and amount in their UPI app before entering their PIN, and tell you once they "
            "have paid; then check the payment status. Do not say the order is placed before that.]")


def summary(items: list[dict[str, Any]]) -> str:
    return json.dumps([{k: v for k, v in i.items() if k not in ("qr", "src")} for i in items], ensure_ascii=False)
