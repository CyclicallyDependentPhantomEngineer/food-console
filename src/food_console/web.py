"""Local web UI: chat with Claude, run any Zomato/Swiggy MCP tool directly, pick dishes by
calories and protein, and see joint stats.

Security model: the server binds to 127.0.0.1 only and checks the Host header (blocks DNS
rebinding). A one-time sign-in link printed in the terminal sets an HttpOnly, SameSite=Strict
session cookie, and every API call must also send an `X-Food-Console: 1` header, which other
websites can't add without a CORS preflight that is never granted (CSRF defence).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import secrets
import sys
import uuid
import webbrowser
from datetime import date
from pathlib import Path
from typing import Any, AsyncIterator

import uvicorn
from dotenv import load_dotenv
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route

from . import _use_ant_profile
from .agent import MODEL, OrderAgent
from .store import Store
from .mcp_bridge import (
    DEFAULT_CONFIG,
    PROVIDERS,
    BridgedTool,
    MCPBridge,
    load_blocked_tools,
    load_read_only_tools,
    load_server_params,
    normalize_schema,
)
from .nutrition import ADDRESS_TOOLS, GOALS, SORTS, describe_address, parse_addresses, pick_dishes, pick_tool_filter, resolve_goal
from .stats import (
    COMPARE_GROUPS,
    PLATFORMS,
    brand_of,
    compare_stats,
    extract_orders,
    history_tool_filter,
    load_orders,
    merge_orders,
    save_orders,
    stats_for_range,
)

STATIC = Path(__file__).with_name("static")
SESSION_COOKIE = "food_console_session"
SIGNED_OUT_PAGE = """<!doctype html><meta charset="utf-8"><title>Food Console</title>
<body style="font:15px system-ui;max-width:520px;margin:15vh auto;padding:0 20px;color:#222">
<h2>Food Console</h2><p>{msg}</p>
<p>Open the newest sign-in link printed in the terminal where <code>food-ui</code> is running.
Each link works once; after it is used a new one is printed for the next browser.</p></body>"""


class AppState:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        # One-time sign-in token for the printed link; it rotates after each use, so a copy
        # left in browser history or shell scrollback can't be replayed.
        self.login_token = secrets.token_urlsafe(24)
        # Session ids handed out as HttpOnly, SameSite=Strict cookies.
        self.sessions: set[str] = set()
        self.mcp: MCPBridge | None = None
        self.store: Store | None = None
        self.chat: OrderAgent | None = None
        self.chat_lock = asyncio.Lock()
        self.stats_lock = asyncio.Lock()
        self.pick_lock = asyncio.Lock()
        self.pending: dict[str, asyncio.Future[bool]] = {}
        # Event sink of the request currently streaming (one chat turn at a time).
        self.sink: asyncio.Queue | None = None

    def busy(self) -> bool:
        return self.chat_lock.locked() or self.stats_lock.locked() or self.pick_lock.locked()

    def login_url(self) -> str:
        return f"http://127.0.0.1:{self.args.port}/login?t={self.login_token}"

    def valid_session(self, sid: str) -> bool:
        return any(secrets.compare_digest(sid, s) for s in self.sessions) if sid else False

    def emit(self, event: dict[str, Any]) -> None:
        if self.sink is not None:
            self.sink.put_nowait(event)

    async def approve(self, tool: BridgedTool, args: dict[str, Any]) -> bool:
        approval_id = uuid.uuid4().hex
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self.pending[approval_id] = fut
        self.emit({"type": "approval", "id": approval_id, "tool": tool.label, "args": args,
                   "description": (tool.tool.description or "")[:600]})
        try:
            return await asyncio.wait_for(fut, timeout=600)
        except TimeoutError:
            return False
        finally:
            self.pending.pop(approval_id, None)

    def new_agent(self, **kwargs) -> OrderAgent:
        assert self.mcp is not None
        return OrderAgent(self.mcp, self.approve, provider=PROVIDERS["all"], profile=self.args.profile,
                          on_event=self.emit, **kwargs)


def _tool_info(t: BridgedTool, agent: OrderAgent) -> dict[str, Any]:
    schema, note = normalize_schema(t.tool.input_schema or {})
    return {
        "name": t.claude_name,
        "label": t.label,
        "brand": brand_of(t.server),
        "server": t.server,
        "tool": t.mcp_name,
        "title": t.tool.title or t.mcp_name,
        "description": t.tool.description or "",
        "note": note,
        "schema": schema,
        "read_only": t.read_only,
        "read_only_configured": t.read_only_configured,
        "offline": t.offline,
        "destructive": t.destructive,
        "needs_confirm": agent.needs_approval(t),
    }


async def _stream(state: AppState, work) -> AsyncIterator[bytes]:
    """Run `work()` while streaming its events as NDJSON; final line is {"type": "done", ...}."""
    queue: asyncio.Queue = asyncio.Queue()
    state.sink = queue
    task = asyncio.create_task(work())
    try:
        while True:
            getter = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait({getter, task}, return_when=asyncio.FIRST_COMPLETED)
            if getter in done:
                yield (json.dumps(getter.result(), ensure_ascii=False, default=str) + "\n").encode()
                continue
            getter.cancel()
            while not queue.empty():
                yield (json.dumps(queue.get_nowait(), ensure_ascii=False, default=str) + "\n").encode()
            try:
                result = task.result()
                yield (json.dumps({"type": "done", **result}, ensure_ascii=False, default=str) + "\n").encode()
            except Exception as exc:
                yield (json.dumps({"type": "error", "message": str(exc)}) + "\n").encode()
            return
    finally:
        state.sink = None
        if not task.done():
            task.cancel()


def build_app(state: AppState) -> Starlette:
    async def login(request: Request) -> Response:
        token = request.query_params.get("t", "")
        if not token or not secrets.compare_digest(token, state.login_token):
            return HTMLResponse(SIGNED_OUT_PAGE.format(
                msg="This sign-in link was already used or is not valid."), status_code=403)
        sid = secrets.token_urlsafe(32)
        state.sessions.add(sid)
        state.login_token = secrets.token_urlsafe(24)
        print(f"A browser signed in. One-time link for another browser: {state.login_url()}")
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(SESSION_COOKIE, sid, httponly=True, samesite="strict", path="/")
        return response

    async def index(request: Request) -> Response:
        if not state.valid_session(request.cookies.get(SESSION_COOKIE, "")):
            return HTMLResponse(SIGNED_OUT_PAGE.format(msg="You are not signed in."), status_code=401)
        return FileResponse(STATIC / "index.html")

    async def status(request: Request) -> Response:
        mcp = state.mcp
        return JSONResponse({
            "model": MODEL,
            "connected": list(mcp.sessions) if mcp else [],
            "failed": mcp.failed if mcp else {},
            "tool_count": len(mcp.tools) if mcp else 0,
            "usage": state.chat.usage if state.chat else {},
        })

    async def reconnect(request: Request) -> Response:
        assert state.mcp
        results = await state.mcp.reconnect()
        return JSONResponse({"results": results, "connected": list(state.mcp.sessions), "failed": state.mcp.failed,
                             "tool_count": len(state.mcp.tools)})

    async def tools(request: Request) -> Response:
        assert state.mcp and state.chat
        return JSONResponse([_tool_info(t, state.chat) for t in state.mcp.tools.values()])

    async def run_tool(request: Request) -> Response:
        assert state.mcp and state.chat
        name = request.path_params["name"]
        tool = state.mcp.tools.get(name)
        if tool is None:
            return JSONResponse({"error": f"unknown tool {name}"}, status_code=404)
        body = await request.json()
        args = body.get("args") or {}
        if not isinstance(args, dict):
            return JSONResponse({"error": "args must be an object"}, status_code=400)
        if state.chat.needs_approval(tool) and body.get("confirm") is not True:
            return JSONResponse({"error": "confirmation required"}, status_code=409)
        payments: list[dict[str, Any]] = []
        try:
            r = await state.mcp.call_ex(name, args)
            text, is_error, source, cached_at, payments = r.text, r.is_error, r.source, r.cached_at, r.payments
        except Exception as exc:
            text, is_error, source, cached_at = f"Tool call failed: {exc}", True, "live", None
        return JSONResponse({"result": text, "is_error": is_error, "source": source, "cached_at": cached_at,
                             "payments": payments})

    async def chat(request: Request) -> Response:
        assert state.chat
        message = ((await request.json()).get("message") or "").strip()
        if not message:
            return JSONResponse({"error": "empty message"}, status_code=400)
        if state.busy():
            return JSONResponse({"error": "busy: another request is running"}, status_code=409)

        async def work():
            async with state.chat_lock:
                return {"text": await state.chat.send(message), "usage": state.chat.usage}

        return StreamingResponse(_stream(state, work), media_type="application/x-ndjson")

    async def chat_reset(request: Request) -> Response:
        assert state.chat
        if state.chat_lock.locked():
            return JSONResponse({"error": "busy"}, status_code=409)
        state.chat.messages.clear()
        return JSONResponse({"ok": True})

    async def approve(request: Request) -> Response:
        body = await request.json()
        fut = state.pending.get(body.get("id", ""))
        if fut is None or fut.done():
            return JSONResponse({"error": "no such pending approval"}, status_code=404)
        fut.set_result(body.get("ok") is True)
        return JSONResponse({"ok": True})

    def _range(request: Request) -> tuple[date | None, date | None]:
        def parse(name: str) -> date | None:
            value = request.query_params.get(name)
            return date.fromisoformat(value) if value else None
        start, end = parse("from"), parse("to")
        if start and end and end < start:
            start, end = end, start
        return start, end

    def _payload(data: dict[str, Any], start: date | None, end: date | None, platform: str = "all") -> dict[str, Any]:
        return {"fetched_at": data.get("fetched_at"), "notes": data.get("notes", ""),
                "added": data.get("added"), **stats_for_range(data, start, end, platform)}

    def _platform(request: Request) -> str:
        platform = request.query_params.get("platform") or "all"
        if platform not in PLATFORMS:
            raise ValueError(f"platform must be one of: {', '.join(PLATFORMS)}")
        return platform

    async def stats(request: Request) -> Response:
        try:
            start, end = _range(request)
            platform = _platform(request)
        except ValueError as exc:
            msg = str(exc) if "platform" in str(exc) else "dates must be YYYY-MM-DD"
            return JSONResponse({"error": msg}, status_code=400)
        data = load_orders()
        if data is None:
            return JSONResponse({"orders": [], "all_count": 0, "metrics": None, "fetched_at": None, "notes": ""})
        return JSONResponse(_payload(data, start, end, platform))

    async def stats_compare(request: Request) -> Response:
        by = request.query_params.get("by") or "platform"
        if by not in COMPARE_GROUPS:
            return JSONResponse({"error": f"by must be one of: {', '.join(COMPARE_GROUPS)}"}, status_code=400)
        try:
            start, end = _range(request)
        except ValueError:
            return JSONResponse({"error": "dates must be YYYY-MM-DD"}, status_code=400)
        data = load_orders()
        if data is None:
            return JSONResponse({"segments": [], "orders": [], "all_count": 0, "fetched_at": None})
        return JSONResponse({"fetched_at": data.get("fetched_at"), "notes": data.get("notes", ""),
                             **compare_stats(data, start, end, by)})

    async def stats_refresh(request: Request) -> Response:
        if state.busy():
            return JSONResponse({"error": "busy: another request is running"}, status_code=409)
        try:
            start, end = _range(request)
            platform = _platform(request)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        async def work():
            async with state.stats_lock:
                # Fresh agent: read-only history tools only, so nothing can change the account.
                agent = state.new_agent(tool_filter=history_tool_filter, allow_orders=False)
                fresh = await extract_orders(agent, limit=state.args.history_limit)
                # Providers only return recent orders, so keep what earlier refreshes found.
                data = merge_orders(load_orders(), fresh)
                save_orders(data)
                return _payload(data, start, end, platform)

        return StreamingResponse(_stream(state, work), media_type="application/x-ndjson")

    async def pick_last(request: Request) -> Response:
        return JSONResponse({"last": state.store.load_pick() if state.store else None,
                             "goals": GOALS, "sorts": SORTS, "platforms": list(PICK_PLATFORMS)})

    async def _saved_addresses(servers: list[str]) -> tuple[list[dict[str, str]], dict[str, str]]:
        """Saved addresses from each picker platform's read-only address tool (no Claude involved)."""
        assert state.mcp
        found: list[dict[str, str]] = []
        problems: dict[str, str] = {}
        for server in servers:
            tool = next((name for name, t in state.mcp.tools.items()
                         if t.server == server and t.mcp_name == ADDRESS_TOOLS.get(server) and t.read_only), None)
            if tool is None:
                problems[server] = "not connected"
                continue
            try:
                r = await state.mcp.call_ex(tool, {})
            except Exception as exc:
                problems[server] = str(exc)[:200]
                continue
            if r.is_error:
                problems[server] = r.text[:200]
                continue
            # A cached fallback is prefixed with a note line; the parser skips it.
            parsed = parse_addresses(server, r.text.split("\n", 1)[1] if r.source == "cache-fallback" else r.text)
            if not parsed:
                problems[server] = "no saved addresses found"
            found.extend(parsed)
        return found, problems

    async def pick_addresses(request: Request) -> Response:
        servers = PICK_PLATFORMS["all"][0]
        found, problems = await _saved_addresses(servers)
        return JSONResponse({"addresses": found, "problems": problems})

    async def pick(request: Request) -> Response:
        if state.busy():
            return JSONResponse({"error": "busy: another request is running"}, status_code=409)
        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be an object"}, status_code=400)
        try:
            goal = resolve_goal(body)
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        platform = body.get("platform") or "all"
        if platform not in PICK_PLATFORMS:
            return JSONResponse({"error": f"platform must be one of: {', '.join(PICK_PLATFORMS)}"}, status_code=400)
        query = str(body.get("query") or "")[:200]
        servers, names = PICK_PLATFORMS[platform]
        address_detail, chosen = "", None
        if body.get("address_id"):
            # Only an address that really is in the user's saved list can be used (it goes into the prompt).
            found, _ = await _saved_addresses([str(body.get("address_server") or "")])
            chosen = next((a for a in found if a["id"] == str(body["address_id"])), None)
            if chosen is None:
                return JSONResponse({"error": "unknown address; reload the address list"}, status_code=400)
            address_detail = describe_address(chosen, servers)

        async def work():
            async with state.pick_lock:
                # Fresh agent restricted to read-only search/menu tools: it can't touch carts or orders.
                agent = state.new_agent(tool_filter=pick_tool_filter(servers), allow_orders=False)
                result = await pick_dishes(agent, goal, query=query, platforms=names, address_detail=address_detail)
                result["platform"] = platform
                if chosen:
                    result["address"] = {k: chosen[k] for k in ("server", "id", "label")}
                if state.store:
                    state.store.save_pick(result)
                return result

        return StreamingResponse(_stream(state, work), media_type="application/x-ndjson")

    allowed_hosts = {f"127.0.0.1:{state.args.port}", f"localhost:{state.args.port}"}

    async def guard(request: Request, call_next):
        if request.headers.get("host") not in allowed_hosts:
            return Response("bad host", status_code=421)
        if request.url.path.startswith("/api/"):
            if not state.valid_session(request.cookies.get(SESSION_COOKIE, "")):
                return JSONResponse({"error": "Not signed in. Open the sign-in link printed in the terminal."}, status_code=401)
            # Custom header: plain cross-site forms can't send it, and cross-origin scripts would
            # need a CORS preflight, which this server never approves (CSRF defence in depth).
            if request.headers.get("x-food-console") != "1":
                return JSONResponse({"error": "missing X-Food-Console header"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
        )
        return response

    return Starlette(
        routes=[
            Route("/", index),
            Route("/login", login),
            Route("/api/status", status),
            Route("/api/tools", tools),
            Route("/api/reconnect", reconnect, methods=["POST"]),
            Route("/api/tools/{name}", run_tool, methods=["POST"]),
            Route("/api/chat", chat, methods=["POST"]),
            Route("/api/chat/reset", chat_reset, methods=["POST"]),
            Route("/api/approve", approve, methods=["POST"]),
            Route("/api/stats", stats),
            Route("/api/stats/refresh", stats_refresh, methods=["POST"]),
            Route("/api/stats/compare", stats_compare),
            Route("/api/pick", pick_last),
            Route("/api/pick/addresses", pick_addresses),
            Route("/api/pick", pick, methods=["POST"]),
        ],
        middleware=[Middleware(BaseHTTPMiddleware, dispatch=guard)],
    )


# Dish picker platforms: server labels to search and how to name them to Claude.
PICK_PLATFORMS = {
    "all": (["zomato", "food"], "Zomato and Swiggy Food"),
    "zomato": (["zomato"], "Zomato"),
    "swiggy": (["food"], "Swiggy Food"),
}

LOG_DIR = Path(__file__).resolve().parents[2] / ".cache" / "logs"
SERVER_NAMES = {"zomato": "Zomato", "food": "Swiggy Food", "instamart": "Swiggy Instamart", "dineout": "Swiggy Dineout",
                "district": "District", "zepto": "Zepto",
                "blinkit": "Blinkit"}


async def _auto_reconnect(mcp: MCPBridge, base: float = 120, cap: float = 900) -> None:
    """Retry offline servers in the background, backing off while they keep failing."""
    delay = base
    while True:
        await asyncio.sleep(delay)
        if not mcp.failed:
            delay = base
            continue
        results = await mcp.reconnect()
        for label, result in results.items():
            name = SERVER_NAMES.get(label, label)
            print(f"{name}: reconnected." if result == "ok" else f"{name}: still offline ({result}).")
        delay = base if all(r == "ok" for r in results.values()) else min(delay * 2, cap)


async def _serve(args: argparse.Namespace) -> None:
    state = AppState(args)
    keys = args.server or [key for _, key in PROVIDERS["all"].servers]
    labels = {key: label for label, key in PROVIDERS["all"].servers}
    servers = [(labels.get(key, key), load_server_params(key, args.config)) for key in keys]
    read_only_tools = {labels.get(key, key): load_read_only_tools(key, args.config) for key in keys}
    blocked_tools = {labels.get(key, key): load_blocked_tools(key, args.config) for key in keys}

    print("Connecting to MCP servers (first run opens a browser login per server) ...")
    print(f"mcp-remote logs: {LOG_DIR}")
    store = Store()
    print(f"Local cache: {store.path}")
    async with MCPBridge(servers, connect_timeout=args.connect_timeout, log_dir=LOG_DIR, require_any=False,
                         store=store, cache_ttl=args.cache_ttl, read_only_tools=read_only_tools,
                         blocked_tools=blocked_tools) as mcp:
        state.mcp = mcp
        state.store = store
        state.chat = state.new_agent()
        if mcp.sessions:
            print(f"Connected: {', '.join(SERVER_NAMES.get(k, k) for k in mcp.sessions)} ({len(mcp.tools)} tools).")
        offline_cached = {t.server for t in mcp.tools.values() if t.offline}
        if offline_cached:
            print(f"Serving cached read-only results for offline: {', '.join(SERVER_NAMES.get(k, k) for k in sorted(offline_cached))}.")
        if mcp.failed:
            print("Offline for now (retried automatically, or click the server in the page header):")
            for label, reason in mcp.failed.items():
                print(f"  - {SERVER_NAMES.get(label, label)}: {reason}")
        retry_task = asyncio.create_task(_auto_reconnect(mcp))

        url = state.login_url()
        config = uvicorn.Config(build_app(state), host="127.0.0.1", port=args.port, log_level="warning")
        server = uvicorn.Server(config)
        print(f"\nOpen: {url}\n(One-time sign-in link: it stops working once used, and a new one is printed "
              "for each further browser. Ctrl+C to stop.)")
        if not args.no_browser:
            with contextlib.suppress(Exception):
                webbrowser.open(url)
        try:
            await server.serve()
        finally:
            retry_task.cancel()


def main() -> None:
    load_dotenv()
    _use_ant_profile()
    p = argparse.ArgumentParser(prog="food-ui", description="Joint Zomato + Swiggy web UI")
    p.add_argument("--port", type=int, default=int(os.environ.get("FOOD_UI_PORT", "8765")))
    p.add_argument("--profile", help="ant profile to use (default: active profile or ANTHROPIC_PROFILE)")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--server", action="append", help="server key(s) from mcp_config.json to use instead of all of them (repeatable)")
    p.add_argument("--connect-timeout", type=float, default=180, help="seconds to wait per server, login included")
    p.add_argument("--history-limit", type=int, default=50, help="max past orders per service for stats")
    p.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    p.add_argument("--cache-ttl", type=float, default=60,
                   help="seconds an identical read-only request is answered from the local cache (0 = only as fallback)")
    args = p.parse_args()
    try:
        asyncio.run(_serve(args))
    except KeyboardInterrupt:
        pass
    sys.exit(0)
