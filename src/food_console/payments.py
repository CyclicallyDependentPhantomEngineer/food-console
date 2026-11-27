"""Payment items (UPI QR codes, UPI intent links, payment links) from account-changing tool results.

The official servers (Swiggy, District, Zepto) expect a chat host that renders their payment
widget. This app shows text, so the QR or link would otherwise never reach the user. Here the
payment artefacts are pulled out of the raw result and shown in the UI instead:

- UPI URIs (`upi://pay?...`, or app-specific ones such as `tez://upi/pay?...`) are normalised
  to `upi://pay` with the same parameters and drawn as a QR locally (the payee and amount are
  exactly what the platform sent; nothing is added);
- QR images the server sends (MCP image content, or `data:image/...` strings) are shown as-is;
- `https` payment links are shown with their host, and as a QR only for known payment hosts.

Nothing here can pay: the user scans or opens the item and authorises it with their UPI PIN.
Only results of tools that are *not* read-only are inspected, so text from menus or search
results can never produce a payment card.
"""

from __future__ import annotations

import base64
import io
import json
import re
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


def qr_data_uri(text: str) -> str:
    """SVG QR code as a data: URI (rendered in an <img>, so it can't run scripts)."""
    import segno

    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="svg", scale=6, border=2, dark="#111", light="#fff")
    return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode()


def upi_item(uri: str) -> dict[str, Any] | None:
    """Normalise a UPI URI and describe who gets paid how much. None if it has no payee."""
    parts = urlsplit(uri.rstrip(".,;"))
    query = parse_qs(parts.query, keep_blank_values=True)
    payee = (query.get("pa") or [""])[0].strip()
    if not payee or "@" not in payee:
        return None
    clean = "upi://pay?" + urlencode({k: v[0] for k, v in query.items()}, safe="@:/")
    amount = (query.get("am") or [""])[0]
    return {
        "kind": "upi",
        "uri": clean,
        "payee_vpa": payee,
        "payee_name": (query.get("pn") or [""])[0],
        "amount_inr": float(amount) if re.fullmatch(r"\d+(\.\d+)?", amount or "") else None,
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


def extract(result: Any) -> list[dict[str, Any]]:
    """Payment items in an MCP CallToolResult (text, images, embedded resources, structured content)."""
    from mcp import types

    texts: list[str] = []
    items: list[dict[str, Any]] = []
    for block in getattr(result, "content", None) or []:
        if isinstance(block, types.TextContent):
            texts.append(block.text)
        elif isinstance(block, types.ImageContent):
            mime = (block.mime_type or "").lower()
            if mime in ("image/png", "image/jpeg", "image/gif", "image/webp") and len(block.data) * 3 // 4 <= MAX_IMAGE_BYTES:
                items.append({"kind": "image", "src": f"data:{mime};base64,{block.data}"})
        elif isinstance(block, types.EmbeddedResource):
            texts.append(getattr(block.resource, "text", None) or "")
    if getattr(result, "structured_content", None) is not None:
        texts.extend(_strings(result.structured_content))
    return extract_from_text("\n".join(texts), items)


def extract_from_text(text: str, items: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
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
        host = (urlsplit(url).hostname or "").lower()
        on = lambda hosts: any(host == h or host.endswith("." + h) for h in hosts)  # noqa: E731
        if url in seen:
            continue
        if not on(GATEWAY_HOSTS) and not (on(SERVICE_HOSTS) and re.search(r"pay|checkout|txn|upi", urlsplit(url).path, re.I)):
            continue  # unknown host, or e.g. an image / tracking URL on a service's own site
        seen.add(url)
        items.append({"kind": "link", "url": url, "host": host, "qr": qr_data_uri(url)})
    return items


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
