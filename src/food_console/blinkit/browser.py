"""Drive blinkit.com with Playwright (system Google Chrome, visible window by default).

Blinkit sits behind a bot check that blocks headless browsers, so a normal, visible Chrome
window is used; if a check ever appears you can solve it in that window. Nothing here tries
to disguise the automation.

Selectors follow what blinkit.com served in Nov 2026 and the original project's choices;
the site changes, so every step reports what it actually saw instead of assuming success.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import session
from .parse import inr, parse_card_text, parse_history_detail, parse_history_summary, valid_product_id

HOME = "https://blinkit.com/"
ORDERS = "https://blinkit.com/account/orders"
QR_PATH = session.SESSION_PATH.with_name("blinkit_payment_qr.png")

CARD = "div[role='button'][id]"
LOCATION_BAR = "div[class*='LocationBar__Container']"
LOCATION_INPUT = "input[name='select-locality'], input[placeholder*='search delivery location']"
LOCATION_RESULT = ("div[class*='LocationSearchList__LocationListContainer'], "
                   "div[class*='LocationSearchBox__LocationItemContainer']")
CART_DRAWER = "div[class*='CartDrawer'], div[class*='CartSidebar'], div.cart-modal-rn, div[class*='CartWrapper__CartContainer']"
CART_BUTTON = "div[class*='CartButton__Button'], div[class*='CartButton__Container']"
ADDRESS_ITEM = "div[class*='AddressList__AddressItemWrapper'], div[class*='AddressListItem__AddressItemWrapper']"
HISTORY_CARD = 'div[class*="BffOrderHistory"] > div > div'
HISTORY_STATUS = re.compile(r"Arrived|Delivered|Cancelled", re.I)
UNAVAILABLE = ["text=Sorry, can't take your order", "text=Currently unavailable", "text=Store is closed"]


def _log(msg: str) -> None:
    print(f"[blinkit] {msg}", file=sys.stderr)  # stdout is the MCP channel


class BlinkitError(RuntimeError):
    pass


class Blinkit:
    def __init__(self, *, headless: bool | None = None, session_path: Path = session.SESSION_PATH):
        self.headless = headless if headless is not None else os.environ.get("BLINKIT_HEADLESS") == "1"
        self.session_path = session_path
        self.lock = asyncio.Lock()
        self._pw = self._browser = self._context = self.page = None
        self.known: dict[str, dict[str, Any]] = {}  # product id -> last seen search info (incl. query)
        self.checkout_total: float | None = None  # total verified by the last checkout()

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self.page is not None and not self.page.is_closed():
            return
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        try:
            self._browser = await self._pw.chromium.launch(channel="chrome", headless=self.headless)
        except Exception as exc:
            raise BlinkitError(f"could not start Google Chrome ({exc}). Install Chrome, or run "
                               "`uv run playwright install chromium`.") from exc
        # No geolocation permission: location comes from a saved address or a place you name,
        # never from a third-party IP lookup.
        self._context = await self._browser.new_context(
            storage_state=session.load_state(self.session_path), viewport={"width": 1280, "height": 900},
            locale="en-IN", timezone_id="Asia/Kolkata",
        )
        self.page = await self._context.new_page()
        await self.page.goto(HOME, wait_until="domcontentloaded", timeout=60000)
        await self.page.wait_for_timeout(2500)
        if "just a moment" in (await self.page.title()).lower():
            _log("Blinkit is showing a bot check; solve it in the Chrome window.")
            await self.page.wait_for_function("!document.title.toLowerCase().includes('just a moment')", timeout=120000)

    async def close(self) -> None:
        for closer in (self._context, self._browser):
            try:
                if closer:
                    await closer.close()
            except Exception:
                pass
        if self._pw:
            await self._pw.stop()
        self._pw = self._browser = self._context = self.page = None

    async def save(self) -> None:
        if self._context:
            session.save_state(await self._context.storage_state(), self.session_path)

    # ------------------------------------------------------------------ helpers
    async def _visible(self, selector: str) -> bool:
        try:
            return await self.page.locator(selector).first.is_visible()
        except Exception:
            return False

    async def _unavailable(self) -> str | None:
        for sel in UNAVAILABLE:
            if await self._visible(sel):
                return sel.removeprefix("text=")
        return None

    async def _dismiss(self) -> None:
        for sel in ("button[aria-label='close']", "button:has-text('✕')", "button:has-text('×')"):
            try:
                if await self._visible(sel):
                    await self.page.locator(sel).first.click(timeout=2000)
                    await self.page.wait_for_timeout(300)
            except Exception:
                pass

    async def _close_cart(self) -> None:
        drawer = self.page.locator(CART_DRAWER).first
        if not await drawer.is_visible():
            return
        close = drawer.locator("[class*='Close'], [class*='close'], [aria-label*='lose']").first
        try:
            if await close.count():
                await close.click(timeout=2000)
                await drawer.wait_for(state="hidden", timeout=3000)
                return
        except Exception:
            pass
        await self.page.reload(wait_until="domcontentloaded")  # Escape doesn't close the drawer
        await self.page.wait_for_timeout(1500)

    async def _open_cart(self) -> bool:
        drawer = self.page.locator(CART_DRAWER).first
        if await drawer.is_visible():
            return True
        btn = self.page.locator(CART_BUTTON).first
        if await btn.count() == 0:
            return False
        await btn.click()
        try:
            await drawer.wait_for(state="visible", timeout=8000)
        except Exception:
            return False
        # The drawer opens empty and fills in a moment later.
        loaded = drawer.locator("div[class*='CartProduct__Container'], div[class*='BillCard'], "
                                "div[class*='CheckoutStrip']").or_(drawer.get_by_text(re.compile("cart is empty", re.I))).first
        try:
            await loaded.wait_for(state="visible", timeout=8000)
        except Exception:
            pass
        return True

    # ------------------------------------------------------------------ account
    async def is_logged_in(self) -> bool:
        try:
            if await self.page.evaluate("""() => { try {
                    const a = JSON.parse(localStorage.getItem('auth') || '{}');
                    return !!(a.accessToken || a.phoneNumber); } catch (e) { return false; } }"""):
                return True
            btn = self.page.locator("div[class*='ProfileButton__Container']").first
            return await btn.count() > 0 and "login" not in (await btn.inner_text()).lower()
        except Exception:
            return False

    async def location(self) -> str:
        bar = self.page.locator(LOCATION_BAR).first
        try:
            return " ".join((await bar.inner_text()).split()) if await bar.count() else ""
        except Exception:
            return ""

    async def status(self) -> dict[str, Any]:
        return {"logged_in": await self.is_logged_in(), "location": await self.location(),
                "location_prompt_open": await self._visible(LOCATION_INPUT)}

    async def send_otp(self, phone: str) -> str:
        if await self.is_logged_in():
            return "Already logged in."
        if await self._visible("text='Login'"):
            await self.page.locator("text='Login'").first.click()
        inp = self.page.locator("input[type='tel'], input[name='mobile']").first
        await inp.wait_for(state="visible", timeout=20000)
        await inp.fill(phone)
        await self.page.wait_for_timeout(400)
        for label in ("Continue", "Next", "Get OTP"):
            if await self._visible(f"button:has-text('{label}')"):
                await self.page.locator(f"button:has-text('{label}')").first.click()
                break
        else:
            await self.page.keyboard.press("Enter")
        await self.page.wait_for_timeout(1500)
        return "OTP requested. Ask the user for the code Blinkit sent by SMS, then call verify_otp."

    async def verify_otp(self, otp: str) -> str:
        boxes = self.page.locator("input[data-test-id='otp-input'], input[inputmode='numeric'], input[type='tel']")
        await boxes.first.wait_for(state="visible", timeout=20000)
        n = await boxes.count()
        if n >= len(otp) > 1:
            for i, digit in enumerate(otp):
                await boxes.nth(i).fill(digit)
        else:
            await boxes.first.fill(otp)
        await self.page.keyboard.press("Enter")
        for _ in range(10):
            await self.page.wait_for_timeout(1000)
            if await self.is_logged_in():
                await self.save()
                return "Logged in. The session is saved (encrypted) for next time."
        return "Login not confirmed yet: the code may be wrong or expired. Check the Chrome window."

    async def logout(self) -> str:
        session.clear(self.session_path)
        if self._context:
            await self._context.clear_cookies()
            await self.page.evaluate("() => { localStorage.clear(); sessionStorage.clear(); }")
        return "Logged out on this machine (saved session deleted)."

    # ------------------------------------------------------------------ location
    def _cart_changed(self) -> None:
        """Anything that can change what is paid voids the total confirmed at checkout."""
        self.checkout_total = None

    async def set_location(self, place: str) -> dict[str, Any]:
        self._cart_changed()
        if not await self._visible(LOCATION_INPUT):
            if await self._visible(LOCATION_BAR):
                await self.page.locator(LOCATION_BAR).first.click()
        inp = self.page.locator(LOCATION_INPUT).first
        await inp.wait_for(state="visible", timeout=15000)
        await inp.fill("")
        await inp.press_sequentially(place, delay=60)  # suggestions only load on real key presses
        result = self.page.locator(LOCATION_RESULT).first
        try:
            await result.wait_for(state="visible", timeout=8000)
        except Exception:
            return {"ok": False, "message": f"No Blinkit location matched {place!r}."}
        chosen = " ".join((await result.inner_text()).split())
        await result.click()
        await self.page.wait_for_timeout(2500)
        await self.save()
        problem = await self._unavailable()
        return {"ok": problem is None, "chosen": chosen, "location": await self.location(),
                **({"warning": f"Blinkit says: {problem}"} if problem else {})}

    async def addresses(self) -> list[dict[str, Any]]:
        if not await self.page.locator(ADDRESS_ITEM).first.is_visible():
            if await self._visible(LOCATION_BAR):
                await self.page.locator(LOCATION_BAR).first.click()
                await self.page.wait_for_timeout(1500)
        items = self.page.locator(ADDRESS_ITEM)
        out = []
        for i in range(await items.count()):
            lines = [ln.strip() for ln in (await items.nth(i).inner_text()).splitlines() if ln.strip()]
            out.append({"index": i, "label": lines[0] if lines else "", "details": " ".join(lines[1:])})
        return out

    async def select_address(self, index: int) -> dict[str, Any]:
        self._cart_changed()
        items = self.page.locator(ADDRESS_ITEM)
        if index < 0 or index >= await items.count():
            return {"ok": False, "message": "No such address; call list_addresses first."}
        await items.nth(index).click()
        await self.page.wait_for_timeout(2000)
        await self.save()
        return {"ok": True, "location": await self.location()}

    # ------------------------------------------------------------------ catalogue
    async def search(self, query: str, limit: int = 20) -> dict[str, Any]:
        from urllib.parse import quote

        await self._dismiss()
        await self._close_cart()
        await self.page.goto(f"{HOME}s/?q={quote(query)}", wait_until="domcontentloaded")
        cards = self.page.locator(CARD).filter(has_text="₹")
        try:
            await cards.first.wait_for(state="visible", timeout=20000)
        except Exception:
            return {"query": query, "results": [], "location": await self.location(),
                    "message": "No products found (or the page did not load)."}
        results = []
        for i in range(min(await cards.count(), limit)):
            card = cards.nth(i)
            pid = await card.get_attribute("id") or ""
            if not valid_product_id(pid):
                continue
            info = parse_card_text(await card.inner_text())
            info["sponsored"] = await card.locator("img[src*='ad_without_bg']").count() > 0
            info["id"] = pid
            self.known[pid] = {"query": query, "name": info["name"]}
            results.append(info)
        out: dict[str, Any] = {"query": query, "results": results, "location": await self.location()}
        if await self._visible(LOCATION_INPUT):
            out["warning"] = ("No delivery location is set, so prices and stock are for a default store. "
                              "Call set_location or select a saved address first.")
        return out

    async def _card(self, product_id: str):
        if not valid_product_id(product_id):
            raise BlinkitError("product_id must be the numeric id from search results")
        card = self.page.locator(f"div[id='{product_id}']")
        if await card.count() == 0 and product_id in self.known:
            await self.search(self.known[product_id]["query"])
            card = self.page.locator(f"div[id='{product_id}']")
        if await card.count() == 0:
            raise BlinkitError("product not on the page; search for it first")
        return card.first

    async def _qty_in_card(self, card) -> int:
        text = await card.inner_text()
        if any(ln.strip().upper() == "ADD" for ln in text.splitlines()):
            return 0
        nums = [int(ln) for ln in (s.strip() for s in text.splitlines()) if ln.isdigit()]
        return nums[-1] if nums else 0

    async def change_quantity(self, product_id: str, delta: int) -> dict[str, Any]:
        self._cart_changed()
        await self._close_cart()
        card = await self._card(product_id)
        before = await self._qty_in_card(card)
        steps = abs(delta)
        for _ in range(steps):
            if delta > 0:
                add = card.locator("div[role='button']:has-text('ADD'), button:has-text('ADD')")
                if await self._qty_in_card(card) == 0 and await add.count():
                    await add.last.click()
                else:
                    await card.locator(".icon-plus").first.locator("..").click()
            else:
                if await self._qty_in_card(card) == 0:
                    break
                await card.locator(".icon-minus").first.locator("..").click()
            await self.page.wait_for_timeout(600)
            if await self._visible("text=Sorry, you can't add more of this item"):
                break
        after = await self._qty_in_card(card)
        problem = await self._unavailable()
        await self.save()
        return {"product_id": product_id, "name": self.known.get(product_id, {}).get("name", ""),
                "quantity_before": before, "quantity_now": after,
                **({"warning": f"Blinkit says: {problem}"} if problem else {})}

    # ------------------------------------------------------------------ cart and checkout
    async def cart(self) -> dict[str, Any]:
        await self._dismiss()
        if not await self._open_cart():
            return {"items": [], "message": "Cart is empty or could not be opened."}
        problem = await self._unavailable()
        data = await self.page.locator(CART_DRAWER).first.evaluate("""(d) => {
            const t = (el, s) => (el.querySelector(s)?.innerText || '').trim();
            // One row per product: CartProduct wraps a DefaultProductCard, so take only the outer one.
            let rows = [...d.querySelectorAll("div[class*='CartProduct__Container']")];
            if (!rows.length) rows = [...d.querySelectorAll("div[class*='DefaultProductCard__Container']")];
            // The quantity is the bare text node between the -/+ icons (the icons are font glyphs).
            const qty = it => { const box = it.querySelector("div[class*='AddToCart__UpdatedButtonContainer']");
              if (!box) return '';
              return [...box.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent.trim()).join(''); };
            const items = rows.map(it => ({ name: t(it, "div[class*='ProductTitle']"), variant: t(it, "div[class*='ProductVariant']"),
                           price: t(it, "div[class*='__Price-']"), mrp: t(it, "div[class*='__Mrp-']"), quantity: qty(it) }))
              .filter(x => x.name);
            const bill = [...d.querySelectorAll("div[class*='BillCard__BillItemContainer']")].map(b => {
              const p = b.innerText.split('\\n').map(s => s.trim()).filter(Boolean);
              // e.g. "Items total ₹150 ₹118" (MRP then price): keep the last amount.
              return p.length >= 2 ? { label: p[0], value: p[p.length - 1].split(/\s+(?=₹)/).pop() } : null; }).filter(Boolean);
            return { items, bill,
              address: [t(d, "div[class*='ListStrip__Heading']"), t(d, "div[class*='ListStrip__SubHeading']")].filter(Boolean).join(' - '),
              total: t(d, "div[class*='CheckoutStrip__NetPriceText']"),
              raw: items.length ? '' : d.innerText.slice(0, 600) };
        }""")
        data["total_inr"] = inr(data.get("total", ""))
        for it in data["items"]:
            it["quantity"] = int(it["quantity"]) if str(it.get("quantity", "")).isdigit() else 1
        if problem:
            data["warning"] = f"Blinkit says: {problem}"
        return data

    async def _check_total(self, expected: float) -> dict[str, Any] | None:
        """Refuse to go on unless the cart total is what the user was shown."""
        c = await self.cart()
        actual = c.get("total_inr")
        if actual is None:
            return {"ok": False, "message": "Could not read the cart total; call view_cart and try again.", "cart": c}
        if abs(actual - float(expected)) > 1:
            return {"ok": False, "message": f"Cart total is ₹{actual:g}, not ₹{expected:g}. Show the user the "
                    "current cart (view_cart) and get their OK on the new total before continuing.", "cart": c}
        return None

    async def checkout(self, expected_total_inr: float) -> dict[str, Any]:
        if (refused := await self._check_total(expected_total_inr)):
            return refused
        proceed = self.page.locator("button, div[role='button']").filter(has_text="Proceed").last
        if not await proceed.is_visible():
            return {"ok": False, "message": "No 'Proceed' button (cart empty, store unavailable, or login needed)."}
        self.checkout_total = float(expected_total_inr)
        await proceed.click()
        await self.page.wait_for_timeout(3000)
        # A second Proceed appears after the address step on some carts.
        if await self._visible("#payment_widget"):
            return {"ok": True, "message": "On the payment page. Ask the user how to pay: 'cod' or 'upi_qr'."}
        return {"ok": True, "message": "Checkout started; if Blinkit asks for an address, use list_addresses / "
                "select_address, then call checkout again.", "location": await self.location()}

    async def place_order(self, payment: str, expected_total_inr: float) -> dict[str, Any]:
        if payment not in ("cod", "upi_qr"):
            return {"ok": False, "message": "payment must be 'cod' or 'upi_qr'"}
        frame_el = await self.page.query_selector("#payment_widget")
        if frame_el is None:
            return {"ok": False, "message": "Not on the payment page; call checkout first."}
        # The payment page hides the cart, so compare with the total checkout() verified.
        if self.checkout_total is None or abs(self.checkout_total - float(expected_total_inr)) > 1:
            return {"ok": False, "message": "expected_total_inr does not match the total confirmed at checkout "
                    f"({'none yet' if self.checkout_total is None else f'₹{self.checkout_total:g}'}). "
                    "Call view_cart and checkout again with the total the user approved."}
        frame = await frame_el.content_frame()
        await frame.wait_for_load_state("networkidle")
        if payment == "cod":
            panel = frame.locator("div[title='Cash']")
            if await panel.count() == 0:
                return {"ok": False, "message": "Cash on delivery is not offered for this order."}
            button = panel.locator("div[role='button']").first
            if await button.get_attribute("aria-disabled") == "true" or await panel.first.get_attribute("disabled") is not None:
                return {"ok": False, "message": "Cash on delivery is disabled for this order; try upi_qr."}
            await button.click()
            await self.page.wait_for_timeout(800)
            pay = self.page.locator("div[class*='Zpayments__Button'], button, div[role='button']").filter(has_text="Pay Now").last
            if not await pay.is_visible():
                pay = frame.locator("button, div[role='button']").filter(has_text="Place Order").last
            if not await pay.is_visible():
                return {"ok": False, "message": "Selected cash, but no 'Pay Now' / 'Place Order' button was found."}
            await pay.click()
            self.checkout_total = None
            await self.page.wait_for_timeout(4000)
            return {"ok": True, "message": "Order placed with cash on delivery. Check order_history to confirm.",
                    "url": self.page.url}
        upi = frame.locator("div[title='UPI']")
        if await upi.count() == 0:
            return {"ok": False, "message": "UPI is not offered for this order."}
        toggle = upi.locator("div[role='button']").first
        if await toggle.get_attribute("aria-expanded") != "true":
            await toggle.click()
        gen = frame.locator("button:has-text('Generate QR')").first
        if await gen.count() == 0:
            return {"ok": False, "message": "UPI opened, but no 'Generate QR' button was found."}
        await gen.click()
        self.checkout_total = None
        img = frame.locator("div[class*='QrImageWrapper'] img").first
        await img.wait_for(state="visible", timeout=10000)
        src = await img.get_attribute("src") or ""
        if not src.startswith("data:image/"):
            return {"ok": True, "message": "QR shown in the Chrome window: the user scans it with a UPI app to pay."}
        fd = os.open(QR_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(base64.b64decode(src.split(",", 1)[1]))
        if sys.platform == "darwin":
            subprocess.run(["open", str(QR_PATH)], check=False)
        return {"ok": True, "message": "UPI QR generated and opened on screen (also visible in the Chrome window). "
                "The order is placed once the user pays it in their UPI app; check order_history after.",
                "qr_file": str(QR_PATH)}

    # ------------------------------------------------------------------ history
    async def order_history(self, count: int = 10) -> dict[str, Any]:
        count = max(1, min(int(count), 30))
        await self.page.goto(ORDERS, wait_until="domcontentloaded")
        cards = self.page.locator(HISTORY_CARD).filter(has_text=HISTORY_STATUS)
        try:
            await cards.first.wait_for(timeout=15000)
        except Exception:
            return {"orders": [], "message": "No past orders found (log in first if you haven't)."}
        orders = []
        for i in range(min(await cards.count(), count)):
            summary: dict[str, str] = {}
            try:
                cards = self.page.locator(HISTORY_CARD).filter(has_text=HISTORY_STATUS)
                card = cards.nth(i)
                summary = parse_history_summary(await card.inner_text())
                await card.locator("div[role='button']").last.click()
                await self.page.wait_for_url("**/account/orders/*/*", timeout=15000)
                await self.page.wait_for_selector("text=/items? in this order/i", timeout=15000)
                order_id = self.page.url.rstrip("/").split("/")[-2]
                detail = parse_history_detail(await self.page.inner_text("body"))
                orders.append({**summary, "order_id": order_id, **detail})
            except Exception as exc:
                orders.append({**summary, "items": [], "error": str(exc)[:120]})
            if i < count - 1:
                await self.page.goto(ORDERS, wait_until="domcontentloaded")
                await self.page.locator(HISTORY_CARD).first.wait_for(timeout=15000)
        return {"orders": orders}
