"""Claude tool-use loop over food-delivery MCP tools, with a human approval gate."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from datetime import date
from typing import Any, Awaitable, Callable

import anthropic

from .mcp_bridge import PROVIDERS, BridgedTool, MCPBridge, Provider
from .credentials import AccountChanged

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
    elif event["type"] == "model":
        print(f"  [model: {event['model']} - {event['reason']}]")
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
        # Per-run token accounting (one run = one send()): every API call and tool result.
        self.on_usage: Callable[[dict[str, Any]], None] | None = None
        self.usage_kind = "chat"
        self.usage_label: str | None = None  # defaults to the user's message
        self._run: dict[str, Any] | None = None
        # Model routing (router.py): model and effort are chosen per run; `tier` is the
        # conversation's current tier (only goes up under automatic routing).
        self.router: Any = None
        self.model_override: str | None = None  # "auto" | "haiku" | "sonnet" | "opus"
        self.model, self.effort, self.tier = MODEL, EFFORT, None
        # When set to a list, every successful tool result is appended as
        # {"server", "tool", "text"} (used by the dish picker to read raw nutrition data).
        self.tool_log: list[dict[str, str]] | None = None
        self.mutation_uncertain = False
        # Optional check on a tool call's arguments; a returned message refuses the call
        # (the dish picker uses it to allow only the user's craving as a search keyword).
        self.arg_guard: Callable[[BridgedTool, dict[str, Any]], str | None] | None = None

    def needs_approval(self, tool: BridgedTool, args: dict[str, Any] | None = None) -> bool:
        # Only local policy can grant an approval exemption; remote hints cannot.
        return not tool.read_only_for(args or {})

    def _tools(self) -> list[dict[str, Any]]:
        return [t.to_claude() for t in self.mcp.tools.values() if self.tool_filter is None or self.tool_filter(t)]

    async def _create(self, output_format: dict[str, Any] | None, max_tokens: int):
        if self.mcp.store is not None:
            self.mcp.store.ensure_identity()
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=max_tokens,
            system=self.system,
            tools=self._tools(),
            messages=self.messages,
            output_config={"effort": self.effort, **({"format": output_format} if output_format else {})},
            cache_control={"type": "ephemeral"},
        )
        if self.model.startswith("claude-opus"):
            # On a safety decline, the API retries the same request on a fallback model
            # (documented for Opus; Claude Haiku 5.5 has no server-side fallback).
            kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
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
        if self.arg_guard is not None and (refusal := self.arg_guard(tool, args)):
            self.on_event({"type": "tool_declined", "tool": tool.label, "reason": refusal})
            return {**result, "content": refusal, "is_error": True}
        if self.needs_approval(tool, args):
            if self.mutation_uncertain:
                return {**result, "content": "A previous action has an uncertain outcome. Check its status and start a new conversation before another write.", "is_error": True}
            if not self.allow_orders:
                return {**result, "content": "Blocked: ordering is disabled in this session.", "is_error": True}
            if not await self.approve(tool, args):
                self.on_event({"type": "tool_declined", "tool": tool.label})
                return {**result, "content": "The user declined this action.", "is_error": True}

        self.on_event({"type": "tool_call", "tool": tool.label, "args": args, "verbose": self.verbose})
        source = "live"
        try:
            r = await self.mcp.call_ex(block.name, args, expected_tool=tool)
            text, is_error, source = r.text, r.is_error, r.source
            if r.outcome_uncertain:
                self.mutation_uncertain = True
            if r.payments:
                self.on_event({"type": "payment", "tool": tool.label, "items": r.payments})
        except AccountChanged:
            raise
        except Exception as exc:  # surface MCP/transport failures to the model, don't crash the chat
            text, is_error = f"Tool call failed: {exc}", True
        self.on_event({"type": "tool_result", "tool": tool.label, "is_error": is_error, "chars": len(text), "source": source})
        if self._run is not None:
            self._run["tools"].append({"tool": tool.label, "chars": len(text), "source": source, "is_error": is_error,
                                       "round": len(self._run["rounds"])})
        if self.tool_log is not None and not is_error:
            self.tool_log.append({"server": tool.server, "tool": tool.mcp_name, "text": text})
        return {**result, "content": text, "is_error": is_error}

    async def send(
        self, user_text: str, *, output_format: dict[str, Any] | None = None, max_tokens: int = 16000
    ) -> str:
        """Send one user message, run tools until Claude finishes, return the final text."""
        if self.mcp.store is not None:
            self.mcp.store.ensure_identity()
        started = time.time()
        if not self.messages:
            self.tier = None  # new conversation: routing starts from the bottom again
        decision = None
        if self.router is not None:
            decision = await self.router.choose(self.client, kind=self.usage_kind, text=user_text,
                                                context=self._recent_context(), floor=self.tier,
                                                override=self.model_override)
            self.model, self.effort, self.tier = decision.model, decision.effort, decision.tier
            self.on_event(decision.as_event())
        self._run = {"kind": self.usage_kind, "label": (self.usage_label or user_text)[:200], "started_at": started,
                     "model": self.model, "effort": self.effort, "tools_offered": len(self._tools()),
                     "history_messages": len(self.messages), "rounds": [], "tools": [],
                     **({"tier": decision.tier, "route_reason": decision.reason, "route_source": decision.source,
                         "router": decision.usage} if decision else {})}
        try:
            return await self._send(user_text, output_format=output_format, max_tokens=max_tokens)
        except AccountChanged:
            self.messages.clear()
            raise
        finally:
            run, self._run = self._run, None
            run["ms"] = round((time.time() - started) * 1000)
            if self.on_usage is not None and (run["rounds"] or run.get("router")):
                try:
                    self.on_usage(run)
                except Exception as exc:  # accounting must never break the chat
                    print(f"warning: could not record token usage: {exc}", file=sys.stderr)

    def _recent_context(self) -> str:
        """Last few plain-text turns, for the router (tool calls and results left out)."""
        parts: list[str] = []
        for m in reversed(self.messages):
            content = m["content"]
            if isinstance(content, str):
                parts.append(f"{m['role']}: {content[:500]}")
            else:
                text = " ".join(getattr(b, "text", "") or (b.get("text", "") if isinstance(b, dict) else "")
                                for b in content if (getattr(b, "type", None) or (b.get("type") if isinstance(b, dict) else None)) == "text")
                if text.strip():
                    parts.append(f"{m['role']}: {text[:500]}")
            if len(parts) >= 3:
                break
        return "\n".join(reversed(parts))

    async def _send(self, user_text: str, *, output_format: dict[str, Any] | None, max_tokens: int) -> str:
        self.messages.append({"role": "user", "content": user_text})

        for _ in range(MAX_TOOL_ROUNDS):
            t0 = time.time()
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
            if self._run is not None:
                self._run["rounds"].append({
                    "model": getattr(response, "model", None) or MODEL,
                    "input": u.input_tokens, "output": u.output_tokens,
                    "cache_read": u.cache_read_input_tokens or 0, "cache_write": u.cache_creation_input_tokens or 0,
                    "stop": response.stop_reason, "ms": round((time.time() - t0) * 1000),
                    "tool_uses": [b.name for b in response.content if b.type == "tool_use"],
                })

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
