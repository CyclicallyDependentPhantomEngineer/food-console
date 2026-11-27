"""Claude tool-use loop over food-delivery MCP tools, with a human approval gate."""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import date
from typing import Any, Awaitable, Callable

import anthropic

from .mcp_bridge import PROVIDERS, BridgedTool, MCPBridge, Provider

MODEL = os.environ.get("ZOMATO_CHAT_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("ZOMATO_CHAT_EFFORT", "medium")
MAX_TOOL_ROUNDS = 25


SYSTEM_PROMPT = """You are a food-ordering and food-analytics assistant connected to the user's \
{brand} account through {brand}'s MCP tools. Available services: {about}.

What you do:
- Find restaurants, dishes and products, compare menus, prices, ratings, and delivery times.
- Build carts and place orders (or bookings) when the user asks.
- Analyse the user's order history: spending over time, favourite restaurants and cuisines, \
order frequency, average order value, and anything else they ask about. Show numbers in INR \
and present tabular results as Markdown tables.

Rules:
- Only use data returned by the tools. If a tool does not give you something, say so; never \
invent restaurants, prices, or order history.
- Before placing an order or booking, show a summary (restaurant or store, items, quantities, \
price breakdown, delivery address or booking slot, payment method) and wait for the user to \
agree in chat. The application also asks the user to approve every order, booking or payment \
tool call; if a call comes back as declined, do not retry it - ask the user what to change.
- Keep replies short and scannable. Today's date is {today}."""

ApproveFn = Callable[[BridgedTool, dict[str, Any]], Awaitable[bool]]
EventFn = Callable[[dict[str, Any]], None]
ToolFilter = Callable[[BridgedTool], bool]


def print_event(event: dict[str, Any]) -> None:
    """Default event sink for the terminal CLI."""
    if event["type"] == "text":
        print(event["text"])
    elif event["type"] == "tool_call":
        args = f"({json.dumps(event['args'], ensure_ascii=False)})" if event.get("verbose") else ""
        print(f"  -> {event['tool']}{args}")
    elif event["type"] == "payment":
        print_payments(event["items"])


def print_payments(items: list[dict[str, Any]]) -> None:
    """Show payment items in the terminal: UPI QR as text art, links as URLs, images in a viewer."""
    import base64
    import subprocess
    import sys

    import segno

    from .store import DB_PATH

    for item in items:
        print("\n" + "=" * 60)
        if item["kind"] == "upi":
            amount = f"₹{item['amount_inr']:g}" if item.get("amount_inr") is not None else "amount not stated"
            print(f"PAY WITH ANY UPI APP: {item.get('payee_name') or ''} <{item['payee_vpa']}>  {amount}")
            segno.make(item["uri"], error="m").terminal(compact=True)
        elif item["kind"] == "link":
            print(f"PAYMENT LINK ({item['host']}): {item['url']}")
        else:
            path = DB_PATH.with_name("payment_qr." + item["src"].split(";")[0].split("/")[-1].replace("svg+xml", "svg"))
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(base64.b64decode(item["src"].split(",", 1)[1]))
            print(f"PAYMENT QR saved to {path}")
            if sys.platform == "darwin":
                subprocess.run(["open", str(path)], check=False)
        print("Check the payee and amount in your UPI app before entering your PIN.")
        print("=" * 60)


class OrderAgent:
    def __init__(
        self,
        mcp: MCPBridge,
        approve: ApproveFn,
        *,
        provider: Provider = PROVIDERS["zomato"],
        allow_orders: bool = True,
        verbose: bool = False,
        profile: str | None = None,
        tool_filter: ToolFilter | None = None,
        on_event: EventFn = print_event,
        system: str | None = None,
    ):
        self.mcp = mcp
        self.approve = approve
        self.allow_orders = allow_orders
        self.verbose = verbose
        self.tool_filter = tool_filter
        self.on_event = on_event
        # No API key: credentials come from the `ant auth login` OAuth profile
        # (the active one, or `profile` / ANTHROPIC_PROFILE when given).
        self.client = anthropic.AsyncAnthropic(profile=profile) if profile else anthropic.AsyncAnthropic()
        self.messages: list[Any] = []
        self.system = system or SYSTEM_PROMPT.format(
            brand=provider.brand, about=provider.about, today=date.today().isoformat()
        )
        if not allow_orders:
            self.system += "\nOrdering and booking are disabled in this session: analyse and browse only."
        self.usage = {"input": 0, "output": 0, "cache_read": 0}
        # When set to a list, every successful tool result is appended as
        # {"server", "tool", "text"} (used by the dish picker to read raw nutrition data).
        self.tool_log: list[dict[str, str]] | None = None

    def needs_approval(self, tool: BridgedTool) -> bool:
        # Fail closed: anything not known to be read-only (server annotation, or the config's
        # readOnlyTools allowlist for servers that send no annotations) needs an explicit "yes".
        # Guessing "dangerous" tools by name missed e.g. Zomato's bind_user_number.
        return not tool.read_only

    def _tools(self) -> list[dict[str, Any]]:
        return [t.to_claude() for t in self.mcp.tools.values() if self.tool_filter is None or self.tool_filter(t)]

    async def _create(self, output_format: dict[str, Any] | None, max_tokens: int):
        kwargs: dict[str, Any] = dict(
            model=MODEL,
            max_tokens=max_tokens,
            system=self.system,
            tools=self._tools(),
            messages=self.messages,
            output_config={"effort": EFFORT, **({"format": output_format} if output_format else {})},
            cache_control={"type": "ephemeral"},
            # On a safety decline, the API retries the same request on a fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if max_tokens <= 16000:
            return await self.client.beta.messages.create(**kwargs)
        # Large outputs: stream so the request doesn't hit HTTP timeouts.
        async with self.client.beta.messages.stream(**kwargs) as stream:
            return await stream.get_final_message()

    async def _run_tool(self, block) -> dict[str, Any]:
        tool = self.mcp.tools.get(block.name)
        args = block.input if isinstance(block.input, dict) else {}
        result: dict[str, Any] = {"type": "tool_result", "tool_use_id": block.id}

        if tool is None or (self.tool_filter is not None and not self.tool_filter(tool)):
            return {**result, "content": f"Unknown tool {block.name}", "is_error": True}
        if self.needs_approval(tool):
            if not self.allow_orders:
                return {**result, "content": "Blocked: ordering is disabled in this session.", "is_error": True}
            if not await self.approve(tool, args):
                self.on_event({"type": "tool_declined", "tool": tool.label})
                return {**result, "content": "The user declined this action.", "is_error": True}

        self.on_event({"type": "tool_call", "tool": tool.label, "args": args, "verbose": self.verbose})
        source = "live"
        try:
            r = await self.mcp.call_ex(block.name, args)
            text, is_error, source = r.text, r.is_error, r.source
            if r.payments:
                self.on_event({"type": "payment", "tool": tool.label, "items": r.payments})
        except Exception as exc:  # surface MCP/transport failures to the model, don't crash the chat
            text, is_error = f"Tool call failed: {exc}", True
        self.on_event({"type": "tool_result", "tool": tool.label, "is_error": is_error, "chars": len(text), "source": source})
        if self.tool_log is not None and not is_error:
            self.tool_log.append({"server": tool.server, "tool": tool.mcp_name, "text": text})
        return {**result, "content": text, "is_error": is_error}

    async def send(
        self, user_text: str, *, output_format: dict[str, Any] | None = None, max_tokens: int = 16000
    ) -> str:
        """Send one user message, run tools until Claude finishes, return the final text."""
        self.messages.append({"role": "user", "content": user_text})

        for _ in range(MAX_TOOL_ROUNDS):
            try:
                response = await self._create(output_format, max_tokens)
            except anthropic.AuthenticationError:
                self.messages.pop()
                return "[Claude auth failed - run `ant auth login` (check with `ant auth status`)]"
            except anthropic.RateLimitError:
                self.messages.pop()
                return "[rate limited - wait a moment and try again]"
            except anthropic.APIStatusError as exc:
                self.messages.pop()
                return f"[API error {exc.status_code}: {exc.message}]"
            except anthropic.APIConnectionError:
                self.messages.pop()
                return "[network error talking to the Claude API]"

            u = response.usage
            self.usage["input"] += u.input_tokens
            self.usage["output"] += u.output_tokens
            self.usage["cache_read"] += u.cache_read_input_tokens or 0

            # Keep the full content (thinking blocks included) so later turns stay valid.
            self.messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "refusal":
                return "[The model declined this request.]"
            if response.stop_reason == "max_tokens":
                return _text(response) + "\n[response cut off at max_tokens]"

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason != "tool_use" or not tool_uses:
                return _text(response)

            text = _text(response)
            if text:
                self.on_event({"type": "text", "text": text})
            # Approval prompts are interactive, so run tools one at a time;
            # all results go back in a single user message.
            results = [await self._run_tool(b) for b in tool_uses]
            self.messages.append({"role": "user", "content": results})

        return "[stopped: too many tool rounds in one turn]"


def _text(response) -> str:
    return "\n".join(b.text for b in response.content if b.type == "text").strip()


async def ask_yes_no(prompt: str) -> bool:
    answer = await asyncio.to_thread(input, prompt)
    return answer.strip().lower() in {"y", "yes"}
