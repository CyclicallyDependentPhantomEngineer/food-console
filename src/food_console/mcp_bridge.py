"""Connect to MCP servers and expose their tools to Claude.

Remote servers (Zomato, Swiggy, District, Zepto) run through mcp-remote, which handles their
OAuth login; local servers (Blinkit) run as a subprocess of this package. Also: read-only
detection and approval policy, the encrypted result cache, payment items in results,
reconnects, and the `disabled` / `readOnlyTools` / `blockedTools` config options."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

from .credentials import AccountChanged
from .payments import extract as extract_payments
from .payments import for_model as payments_for_model
from .payments import swiggy_cart

MAX_TOOL_RESULT_BYTES = 2_000_000
MAX_TOOL_ARGS_BYTES = 64_000
TOOL_TIMEOUT = 120

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "mcp_config.json"


@dataclass(frozen=True)
class Provider:
    brand: str
    # (tool-name prefix, server key in mcp_config.json)
    servers: tuple[tuple[str, str], ...]
    about: str


PROVIDERS: dict[str, Provider] = {
    "zomato": Provider(
        brand="Zomato",
        servers=(("zomato", "zomato-mcp"),),
        about="Zomato food delivery",
    ),
    "swiggy": Provider(
        brand="Swiggy",
        servers=(
            ("food", "swiggy-food"),
            ("instamart", "swiggy-instamart"),
            ("dineout", "swiggy-dineout"),
        ),
        about=(
            "Swiggy Food (restaurant delivery, tools prefixed food_), Swiggy Instamart "
            "(grocery delivery, tools prefixed instamart_) and Swiggy Dineout (restaurant "
            "table bookings, tools prefixed dineout_)"
        ),
    ),
    "district": Provider(
        brand="District",
        servers=(("district", "district-mcp"),),
        about="District by Zomato (restaurant dining and table bookings, movies and events)",
    ),
    "zepto": Provider(
        brand="Zepto",
        servers=(("zepto", "zepto-mcp"),),
        about="Zepto (groceries and essentials delivered in minutes, plus Zepto Cafe food)",
    ),
    "blinkit": Provider(
        brand="Blinkit",
        servers=(("blinkit", "blinkit-mcp"),),
        about="Blinkit (groceries in minutes; unofficial, drives blinkit.com in a Chrome window on this machine)",
    ),
}
PROVIDERS["all"] = Provider(
    brand="Zomato, Swiggy, District, Zepto and Blinkit",
    servers=(PROVIDERS["zomato"].servers + PROVIDERS["swiggy"].servers + PROVIDERS["district"].servers
             + PROVIDERS["zepto"].servers + PROVIDERS["blinkit"].servers),
    about=(
        f"{PROVIDERS['zomato'].about} (tools prefixed zomato_); {PROVIDERS['swiggy'].about}; "
        f"{PROVIDERS['district'].about} (tools prefixed district_); "
        f"{PROVIDERS['zepto'].about} (tools prefixed zepto_); "
        f"{PROVIDERS['blinkit'].about} (tools prefixed blinkit_)"
    ),
)

# Which brand each server label belongs to.
SERVER_BRANDS = {"zomato": "Zomato", "food": "Swiggy", "instamart": "Swiggy", "dineout": "Swiggy", "district": "District", "zepto": "Zepto", "blinkit": "Blinkit"}

# Read-only tools whose answer changes from moment to moment (payment or delivery status,
# carts, OTP state): never answered from the fresh-hit cache, so polling sees real updates.
# The cache is still used as a clearly-marked fallback when the server is unreachable.
_LIVE_FIRST = re.compile(r"status|track|payment|cart|otp|live", re.I)

# Claude tool names must match ^[a-zA-Z0-9_-]{1,64}$.
_INVALID_NAME_CHARS = re.compile(r"[^a-zA-Z0-9_-]")


def load_server_params(server: str, config_path: Path = DEFAULT_CONFIG) -> StdioServerParameters:
    config = json.loads(config_path.read_text())
    entry = config["mcpServers"][server]
    # "${python}" = this interpreter, for servers that ship inside this package (e.g. Blinkit).
    command = sys.executable if entry["command"] == "${python}" else entry["command"]
    args = entry.get("args", [])
    if "${mcp_remote}" in args:
        proxy = config_path.resolve().parent / "node_modules/mcp-remote/dist/proxy.js"
        if not proxy.is_file():
            raise RuntimeError("MCP runtime missing. Run npm ci --ignore-scripts in the config directory first.")
        args = [str(proxy) if arg == "${mcp_remote}" else arg for arg in args]
    return StdioServerParameters(
        command=command,
        args=args,
        env={**{k: os.environ[k] for k in ("MCP_REMOTE_CONFIG_DIR", "BLINKIT_HEADLESS") if k in os.environ},
             **(entry.get("env") or {})},
    )


def disabled_servers(config_path: Path = DEFAULT_CONFIG) -> set[str]:
    """Servers marked `"disabled": true` in the config. They are left out of every default set
    (food-ui, `--provider all`, `food-auth --app all`) but still run when named explicitly
    (e.g. `--server blinkit-mcp`, `blinkit-chat`, `food-auth --app blinkit`)."""
    config = json.loads(config_path.read_text())
    return {key for key, entry in config["mcpServers"].items() if isinstance(entry, dict) and entry.get("disabled")}


def default_keys(keys: list[str], config_path: Path = DEFAULT_CONFIG) -> list[str]:
    """`keys` without the servers disabled in the config."""
    off = disabled_servers(config_path)
    return [k for k in keys if k not in off]


def load_read_only_tools(server: str, config_path: Path = DEFAULT_CONFIG) -> set[str]:
    """The local read-only policy for this server (empty means every tool needs approval)."""
    config = json.loads(config_path.read_text())
    return set(config["mcpServers"][server].get("readOnlyTools", []))


def load_blocked_tools(server: str, config_path: Path = DEFAULT_CONFIG) -> set[str]:
    """The `blockedTools` list for a server: tools never offered to Claude or the Actions tab
    (e.g. ways to pay without the user's UPI PIN)."""
    config = json.loads(config_path.read_text())
    return set(config["mcpServers"][server].get("blockedTools", []))


def server_url(params: StdioServerParameters) -> str | None:
    """The remote MCP URL that mcp-remote is pointed at, if any."""
    return next((a for a in params.args if a.startswith(("https://", "http://"))), None)


def auth_dir() -> Path:
    """Where mcp-remote caches OAuth state (overridable with MCP_REMOTE_CONFIG_DIR)."""
    return Path(os.environ.get("MCP_REMOTE_CONFIG_DIR") or Path.home() / ".mcp-auth")


def auth_files(url: str) -> list[Path]:
    """mcp-remote's cached files for one server: <md5(url)>_tokens.json, _client_info.json, ..."""
    server_hash = hashlib.md5(url.encode()).hexdigest()
    base = auth_dir()
    if not base.is_dir():
        return []
    return sorted(p for p in base.glob(f"mcp-remote-*/{server_hash}_*") if p.is_file())


def remote_pids(url: str) -> set[int]:
    """PIDs of running mcp-remote processes for this server URL (npx wrapper and node child)."""
    try:
        out = subprocess.run(
            ["pgrep", "-f", rf"(mcp-remote(@[^ ]*)?|mcp-remote/dist/proxy\.js) {re.escape(url)}( |$)"], capture_output=True, text=True, check=False
        ).stdout
    except FileNotFoundError:  # no pgrep on this system
        return set()
    return {int(p) for p in out.split()} - {os.getpid()}


def stop_pids(pids: set[int], grace: float = 3.0) -> None:
    """SIGTERM, then SIGKILL whatever is still alive after `grace` seconds."""
    for pid in pids:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    alive = set(pids)
    while alive and time.monotonic() < deadline:
        time.sleep(0.1)
        alive = {p for p in alive if _alive(p)}
    for pid in alive:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers signal 0; reap it if it is our child.
    with contextlib.suppress(ChildProcessError):
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return False
    return True


def stop_new_remotes(url: str, before: set[int]) -> set[int]:
    """Stop mcp-remote processes for `url` that were not running at `before` (i.e. ones we started).

    Interrupting a login (timeout, Ctrl+C) can leave mcp-remote's node process behind
    holding the OAuth callback port; every later login for that server then waits on it.
    """
    leftover = remote_pids(url) - before
    if leftover:
        stop_pids(leftover)
    return leftover


def logout(url: str) -> list[Path]:
    """Delete the cached OAuth tokens and client registration for one server. Returns removed files."""
    removed = []
    for path in auth_files(url):
        path.unlink()
        removed.append(path)
    if removed:
        from .store import clear_private_cache
        clear_private_cache()
    return removed


@dataclass
class BridgedTool:
    server: str
    mcp_name: str
    claude_name: str
    tool: types.Tool
    session: ClientSession | None  # None: server offline, tool known from the local cache
    allowlisted: bool = False  # local policy, never a remote approval grant
    read_operations: dict[str, Any] = field(default_factory=dict)

    def read_only_for(self, arguments: dict[str, Any]) -> bool:
        if self.read_only:
            return True
        key = self.read_operations.get("argument")
        value = arguments.get(key) if key else None
        return isinstance(value, str) and value in self.read_operations.get("values", [])

    @property
    def offline(self) -> bool:
        return self.session is None

    @property
    def label(self) -> str:
        return f"{self.server}:{self.mcp_name}"

    @property
    def read_only(self) -> bool:
        return self.allowlisted

    @property
    def read_only_configured(self) -> bool:
        return self.allowlisted

    @property
    def destructive(self) -> bool:
        ann = self.tool.annotations
        return bool(ann and ann.destructive_hint)

    def to_claude(self) -> dict[str, Any]:
        schema, note = normalize_schema(self.tool.input_schema or {})
        description = self.tool.description or self.tool.title or self.mcp_name
        if note:
            description = f"{description}\n\n{note}"
        return {
            "name": self.claude_name,
            "description": description[:4000],
            "input_schema": schema,
        }


_COMBINATORS = ("allOf", "anyOf", "oneOf")


def normalize_schema(raw: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Make an MCP input schema acceptable to the Claude API.

    Claude requires a top-level ``{"type": "object"}`` schema without
    ``allOf``/``anyOf``/``oneOf``. Those combinators are flattened: the branches'
    properties are merged into one object; ``allOf`` requires every field any
    branch requires, while ``anyOf``/``oneOf`` require only fields common to all
    branches. The alternatives that no longer fit in the schema are returned as a
    note for the tool description, so the model still knows the rule.
    """
    schema = copy.deepcopy(raw)
    notes: list[str] = []
    properties: dict[str, Any] = schema.setdefault("properties", {})
    required: set[str] = set(schema.get("required", []))

    for key in _COMBINATORS:
        branches = schema.pop(key, None)
        if not isinstance(branches, list) or not branches:
            continue
        branch_required: list[set[str]] = []
        for branch in branches:
            branch = _resolve_ref(branch, schema)
            if not isinstance(branch, dict):
                continue
            for name, prop in (branch.get("properties") or {}).items():
                properties.setdefault(name, prop)
            branch_required.append(set(branch.get("required", [])))
        if not branch_required:
            continue
        if key == "allOf":
            required |= set().union(*branch_required)
        else:
            common = set.intersection(*branch_required)
            required |= common
            options = [sorted(r - common) for r in branch_required if r - common]
            if options:
                word = "exactly one" if key == "oneOf" else "at least one"
                sets = " | ".join("{" + ", ".join(o) + "}" for o in options)
                notes.append(f"Input rule: provide {word} of these field sets: {sets}.")

    schema["type"] = "object"
    if required:
        schema["required"] = sorted(required)
    return schema, " ".join(notes)


def _resolve_ref(branch: Any, root: dict[str, Any]) -> Any:
    """Resolve a local ``#/$defs/X`` or ``#/definitions/X`` reference; anything else is returned as is."""
    if not isinstance(branch, dict) or "$ref" not in branch:
        return branch
    ref = branch["$ref"]
    for prefix in ("#/$defs/", "#/definitions/"):
        if isinstance(ref, str) and ref.startswith(prefix):
            return (root.get(prefix[2:-1]) or {}).get(ref[len(prefix):], branch)
    return branch


class MCPBridge:
    """Async context manager holding live MCP sessions to one or more servers.

    With a single server, tool names are passed through unchanged. With several,
    each is prefixed with its server label (e.g. ``food_search``) so names can't clash.

    Each connection lives in its own background task: the MCP client must be opened and
    closed in the same task, and this lets `reconnect()` be called later from any task
    (e.g. a web request) without breaking shutdown.
    """

    def __init__(
        self,
        servers: list[tuple[str, StdioServerParameters]],
        connect_timeout: float | None = None,
        *,
        log_dir: Path | None = None,
        require_any: bool = True,
        store: Any = None,
        cache_ttl: float = 60,
        read_only_tools: dict[str, set[str]] | None = None,
        blocked_tools: dict[str, set[str]] | None = None,
        read_only_operations: dict[str, dict] | None = None,
        payment_tools: dict[str, dict] | None = None,
        tool_timeout: float = TOOL_TIMEOUT,
    ):
        self._servers = servers
        self._params = dict(servers)
        # Covers the first-run browser login, so keep it generous.
        self._connect_timeout = connect_timeout
        # When set, mcp-remote's (very verbose) stderr goes to <log_dir>/<label>.log.
        self._log_dir = log_dir
        self._require_any = require_any
        # Optional store.Store: read-only results are cached there and served when a server
        # is offline/rate-limited; identical reads within `cache_ttl` seconds skip the server.
        self.store = store
        self.cache_ttl = cache_ttl
        # Only this local policy can grant an approval exemption.
        self.read_only_tools = read_only_tools or {}
        self.blocked_tools = blocked_tools or {}
        self.read_only_operations = read_only_operations or {}
        self.payment_tools = payment_tools or {}
        self.tool_timeout = tool_timeout
        self.uncertain_servers: set[str] = set()
        self._write_locks: dict[str, asyncio.Lock] = {}
        self.sessions: dict[str, ClientSession] = {}
        self.failed: dict[str, str] = {}
        self.tools: dict[str, BridgedTool] = {}
        self._conns: dict[str, tuple[asyncio.Task, asyncio.Event]] = {}
        self._ours: dict[str, set[int]] = {}
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "MCPBridge":
        # Connect one at a time: each first-time server opens its own browser login,
        # and parallel mcp-remote logins compete for the same OAuth callback port.
        try:
            for label, params in self._servers:
                try:
                    await self._open(label, params)
                except Exception as exc:
                    if len(self._servers) == 1:
                        raise
                    self._mark_failed(label, exc)
            if not self.sessions and self._require_any:
                raise RuntimeError("could not connect to any MCP server")
            await self.refresh_tools()
        except BaseException:
            await self.aclose()
            raise
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        conns, self._conns = list(self._conns.values()), {}
        for _, stop in conns:
            stop.set()
        tasks = [t for t, _ in conns]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=10)
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.wait(pending, timeout=5)
        self.sessions.clear()
        self._stop_leftovers()

    async def reconnect(self, labels: list[str] | None = None) -> dict[str, str]:
        """Try again to connect servers that are offline. Returns {label: "ok" | reason}."""
        async with self._lock:
            results: dict[str, str] = {}
            for label in labels or list(self.failed):
                if label in self.sessions or label not in self._params:
                    continue
                try:
                    await self._open(label, self._params[label])
                    self.failed.pop(label, None)
                    results[label] = "ok"
                except Exception as exc:
                    results[label] = self._mark_failed(label, exc)
            await self.refresh_tools()
            return results

    def _mark_failed(self, label: str, exc: BaseException) -> str:
        reason = self._reason(label, exc)
        self.failed[label] = reason
        if self._log_dir is None:  # terminal apps; food-ui prints its own summary
            print(f"warning: could not connect to {label}: {reason}", file=sys.stderr)
        return reason

    def _log_path(self, label: str) -> Path | None:
        return self._log_dir / f"{label}.log" if self._log_dir else None

    def _reason(self, label: str, exc: BaseException) -> str:
        """A short, human-readable reason, using mcp-remote's log when we have it."""
        tail = ""
        path = self._log_path(label)
        if path and path.exists():
            with path.open("rb") as fh:
                fh.seek(max(0, path.stat().st_size - 6000))
                tail = fh.read().decode("utf-8", "replace")
            tail = tail[tail.rfind("--- connect ") :]  # only this attempt
        if "429" in tail or "Too Many Requests" in tail:
            return "rate-limited by the server (HTTP 429); retrying automatically in a few minutes"
        if isinstance(exc, TimeoutError):
            return "timed out - finish the browser login, or run `food-auth login` first"
        if "401" in tail or "invalid_token" in tail or "Unauthorized" in tail:
            return "not logged in or the login expired - run `food-auth login`"
        leaf = exc
        while isinstance(leaf, BaseExceptionGroup) and leaf.exceptions:
            leaf = leaf.exceptions[0]
        return str(leaf) or type(leaf).__name__

    async def _open(self, label: str, params: StdioServerParameters) -> ClientSession:
        url = server_url(params)
        before = remote_pids(url) if url else set()
        loop = asyncio.get_running_loop()
        ready: asyncio.Future[ClientSession] = loop.create_future()
        stop = asyncio.Event()
        path = self._log_path(label)
        errlog = None
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(path.parent, 0o700)  # logs can include request metadata: owner-only
            errlog = path.open("a", encoding="utf-8")
            os.chmod(path, 0o600)
            errlog.write(f"\n--- connect {datetime.now().isoformat(timespec='seconds')} ---\n")
            errlog.flush()

        async def hold() -> None:
            try:
                async with stdio_client(params, errlog=errlog or sys.stderr) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        if not ready.done():
                            ready.set_result(session)
                        await stop.wait()
            except BaseException as exc:
                if not ready.done():
                    if isinstance(exc, Exception):
                        ready.set_exception(exc)
                    else:
                        ready.cancel()
                elif not stop.is_set() and isinstance(exc, Exception):
                    # The server went away mid-session: show it as offline so it can be retried.
                    self.sessions.pop(label, None)
                    self.failed[label] = f"disconnected: {self._reason(label, exc)}"
                if not isinstance(exc, Exception):
                    raise
            finally:
                if errlog:
                    errlog.close()

        task = asyncio.create_task(hold(), name=f"mcp:{label}")
        try:
            session = await asyncio.wait_for(asyncio.shield(ready), self._connect_timeout)
        except BaseException:
            task.cancel()
            await asyncio.wait({task}, timeout=5)
            if url:  # don't leave a half-finished login holding the callback port
                stop_new_remotes(url, before)
            raise
        self._conns[label] = (task, stop)
        if url:
            # Remember the processes this connection started, so exit can stop
            # them if they outlive the session - without touching other apps' ones.
            self._ours[url] = remote_pids(url) - before
        self.sessions[label] = session
        return session

    def _stop_leftovers(self) -> None:
        for url, pids in self._ours.items():
            alive = pids & remote_pids(url)
            if alive:
                stop_pids(alive)
        self._ours.clear()

    async def refresh_tools(self) -> None:
        prefix = len(self._servers) > 1
        tools: dict[str, BridgedTool] = {}
        for label, session in list(self.sessions.items()):
            try:
                result = await asyncio.wait_for(session.list_tools(), self.tool_timeout)
            except Exception as exc:  # connection dropped since it was opened
                self.sessions.pop(label, None)
                self.failed[label] = f"disconnected: {self._reason(label, exc)}"
                continue
            # Sorted so the tool list is byte-stable across runs (keeps prompt cache warm).
            for tool in sorted(result.tools, key=lambda t: t.name):
                if tool.name in self.blocked_tools.get(label, ()):
                    continue
                raw = f"{label}_{tool.name}" if prefix else tool.name
                claude_name = _INVALID_NAME_CHARS.sub("_", raw)[:64]
                if claude_name in tools:
                    print(f"warning: tool name clash on {claude_name}; skipping {label}:{tool.name}", file=sys.stderr)
                    continue
                tools[claude_name] = BridgedTool(label, tool.name, claude_name, tool, session,
                                                 tool.name in self.read_only_tools.get(label, ()),
                                                 self.read_only_operations.get(label, {}).get(tool.name, {}))
            if self.store is not None:
                self.store.save_tool_defs(label, [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in result.tools])
        # Offline servers: offer their read-only tools from the cached tool list, so cached
        # results can still be used (account-changing tools need a live connection).
        if self.store is not None:
            for label, _ in self._servers:
                if label in self.sessions:
                    continue
                for d in self.store.load_tool_defs(label):
                    try:
                        tool = types.Tool.model_validate(d)
                    except Exception:
                        continue
                    raw = f"{label}_{tool.name}" if prefix else tool.name
                    claude_name = _INVALID_NAME_CHARS.sub("_", raw)[:64]
                    bt = BridgedTool(label, tool.name, claude_name, tool, None, tool.name in self.read_only_tools.get(label, ()),
                                                 self.read_only_operations.get(label, {}).get(tool.name, {}))
                    if (bt.read_only or bt.read_operations) and tool.name not in self.blocked_tools.get(label, ()) and claude_name not in tools:
                        tools[claude_name] = bt
        self.tools = dict(sorted(tools.items(), key=lambda kv: (self._order(kv[1].server), kv[0])))

    def _order(self, label: str) -> int:
        return next((i for i, (l, _) in enumerate(self._servers) if l == label), len(self._servers))

    def claude_tools(self) -> list[dict[str, Any]]:
        return [t.to_claude() for t in self.tools.values()]

    async def call(self, claude_name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """Run a tool. Returns (text_for_claude, is_error)."""
        r = await self.call_ex(claude_name, arguments)
        return r.text, r.is_error

    async def prepare_review(self, tool: BridgedTool, arguments: dict[str, Any]) -> ActionReview | None:
        policy = self.payment_tools.get(tool.server, {}).get(tool.mcp_name, {})
        if policy.get("adapter") != "swiggy-food":
            return None
        if tool.mcp_name != "place_food_order":
            raise ValueError("Swiggy adapter only supports place_food_order")
        cart_tool = next((t for t in self.tools.values() if t.server == tool.server
                          and t.mcp_name == "get_food_cart" and t.read_only), None)
        if not cart_tool or cart_tool.offline or tool.offline:
            raise ValueError("A live Swiggy cart is required before reviewing checkout")
        cart = await self._review_cart(cart_tool, arguments)
        return ActionReview(tool, cart_tool, copy.deepcopy(arguments), cart, time.monotonic() + 120)

    async def _review_cart(self, tool: BridgedTool, arguments: dict[str, Any]) -> dict:
        if self.store:
            self.store.ensure_identity()
        raw = await asyncio.wait_for(tool.session.call_tool("get_food_cart", {"addressId": arguments["addressId"]}),
                                     self.tool_timeout)
        if self.store:
            self.store.ensure_identity()
        if len(raw.model_dump_json().encode()) > MAX_TOOL_RESULT_BYTES:
            raise ValueError("cart response exceeds the size limit")
        return swiggy_cart(raw, arguments)

    async def call_ex(self, claude_name: str, arguments: dict[str, Any], *,
                      expected_tool: BridgedTool | None = None, review: ActionReview | None = None) -> ToolCall:
        tool = self.tools.get(claude_name)
        if tool and not tool.read_only_for(arguments):
            # Serialize local cart edits and checkout, including its final live check.
            lock = self._write_locks.setdefault(tool.server, asyncio.Lock())
            async with lock:
                return await self._call_ex(claude_name, arguments, expected_tool=expected_tool, review=review)
        return await self._call_ex(claude_name, arguments, expected_tool=expected_tool, review=review)

    async def _call_ex(self, claude_name: str, arguments: dict[str, Any], *,
                       expected_tool: BridgedTool | None = None, review: ActionReview | None = None) -> ToolCall:
        """Run a tool, using the local cache for read-only tools when that helps.

        - read-only + identical call within `cache_ttl`: served from cache (no request), except
          status/tracking/payment/cart tools, which always ask the server;
        - read-only + server offline, rate-limited or failing: last cached result, clearly marked;
        - anything else goes to the server; a successful account-changing call clears that
          server's cached results so e.g. a cart is never shown stale afterwards.
        """
        bridged = self.tools.get(claude_name)
        if bridged is None:
            return ToolCall(f"Unknown tool: {claude_name}", True)
        if expected_tool is not None and bridged is not expected_tool:
            return ToolCall("Tool definition changed after review; review the action again.", True)
        if not isinstance(arguments, dict) or len(json.dumps(arguments, allow_nan=False).encode()) > MAX_TOOL_ARGS_BYTES:
            return ToolCall("Tool arguments exceed the size limit or are not an object.", True)
        store = self.store
        if store is not None:
            store.ensure_identity()
        read_only = bridged.read_only_for(arguments)
        if not read_only and bridged.server in self.uncertain_servers:
            return ToolCall("A previous action has an uncertain outcome. Fetch live order/payment status before another write.", True)
        policy = self.payment_tools.get(bridged.server, {}).get(bridged.mcp_name, {})
        if policy.get("adapter") == "swiggy-food":
            if (not review or review.used or review.expires <= time.monotonic()
                    or review.tool is not bridged or review.arguments != arguments
                    or self.tools.get(review.cart_tool.claude_name) is not review.cart_tool):
                return ToolCall("Review the current Swiggy cart before placing this order.", True)
            review.used = True
            try:
                current_cart = await self._review_cart(review.cart_tool, arguments)
                if current_cart != review.cart:
                    return ToolCall("The cart changed after approval. Review the updated cart before placing the order.", True)
                if (review.expires <= time.monotonic() or self.tools.get(claude_name) is not bridged
                        or self.tools.get(review.cart_tool.claude_name) is not review.cart_tool):
                    return ToolCall("Review expired or tools changed while checking the cart. Review the action again.", True)
            except AccountChanged:
                raise
            except Exception:
                return ToolCall("Could not recheck the live cart. No order was submitted; review it again when connected.", True)
        cacheable = read_only and store is not None
        cached = store.get_result(bridged.server, bridged.mcp_name, arguments) if cacheable else None

        fresh_ok = self.cache_ttl > 0 and not bridged.offline and not _LIVE_FIRST.search(bridged.mcp_name)
        if cached and fresh_ok and cached.age_seconds <= self.cache_ttl:
            return ToolCall(cached.text, False, "cache", cached.fetched_iso)
        if bridged.offline:
            why = self.failed.get(bridged.server, "offline")
            if cached and read_only:
                return self._from_cache(cached, f"{bridged.server} is offline ({why})")
            return ToolCall(f"{bridged.server} is offline ({why}) and there is no cached result for this "
                            "request yet. Tell the user it can't be fetched right now.", True)
        try:
            raw = await asyncio.wait_for(bridged.session.call_tool(bridged.mcp_name, arguments), self.tool_timeout)
            # A write has already happened. Invalidate before parsing or presentation.
            if not read_only and store is not None:
                store.invalidate(bridged.server)
            if store is not None:
                store.ensure_identity()
            if len(raw.model_dump_json().encode()) > MAX_TOOL_RESULT_BYTES:
                if read_only:
                    return ToolCall("Tool result exceeds the size limit.", True)
                self.uncertain_servers.add(bridged.server)
                return ToolCall("Action returned an oversized result. Check order/payment status before any retry.",
                                False, outcome_uncertain=True)
            text, is_error = _render_result(raw)
        except asyncio.CancelledError:
            if not read_only:
                self.uncertain_servers.add(bridged.server)
                if store is not None:
                    with contextlib.suppress(AccountChanged):
                        store.invalidate(bridged.server)
            raise
        except AccountChanged:
            if not read_only:
                self.uncertain_servers.add(bridged.server)
                return ToolCall("Account credentials changed during the action. It may have succeeded; restart and check order/payment status before retrying.", False, outcome_uncertain=True)
            raise
        except Exception as exc:
            if not read_only:
                self.uncertain_servers.add(bridged.server)
                if store is not None:
                    store.invalidate(bridged.server)
                return ToolCall(f"Action outcome unknown ({type(exc).__name__}). Check order/payment status before retrying.",
                                False, outcome_uncertain=True)
            if cached:
                return self._from_cache(cached, f"the request to {bridged.server} failed ({exc})")
            raise
        if is_error and _RATE_LIMIT.search(text):
            if cached:
                return self._from_cache(cached, f"{bridged.server} is rate-limiting requests")
            return ToolCall(text, True)
        payments: list[dict[str, Any]] = []
        if not read_only and not is_error:
            policy = self.payment_tools.get(bridged.server, {}).get(bridged.mcp_name)
            if policy:
                try:
                    payments = extract_payments(raw, policy=policy, arguments=arguments,
                                                reviewed_cart=review.cart if review else None)
                    text = payments_for_model(text, payments)
                except Exception:
                    # Never turn a completed order into a tool failure or a retry.
                    text += "\n[Action returned; payment display failed validation. Check status and complete payment in the provider app. Do not repeat the action.]"
        if not is_error and cacheable:
            store.put_result(bridged.server, bridged.mcp_name, arguments, text)
        if not is_error and read_only and re.search(r"status|track|order.*history|orders|order_detail", bridged.mcp_name, re.I):
            self.uncertain_servers.discard(bridged.server)
        return ToolCall(text, is_error, payments=payments)

    @staticmethod
    def _from_cache(cached: Any, why: str) -> "ToolCall":
        age = cached.age_seconds
        ago = f"{age / 60:.0f} min" if age < 5400 else f"{age / 3600:.1f} h" if age < 172800 else f"{age / 86400:.0f} days"
        note = (f"[Cached result from {cached.fetched_iso} ({ago} ago), served because {why}. "
                "It may be out of date - say so if that matters.]\n")
        return ToolCall(note + cached.text, False, "cache-fallback", cached.fetched_iso)


_RATE_LIMIT = re.compile(r"\b429\b|too many requests|rate[ -]?limit", re.I)


@dataclass
class ActionReview:
    tool: BridgedTool
    cart_tool: BridgedTool
    arguments: dict[str, Any]
    cart: dict[str, Any]
    expires: float
    used: bool = False


@dataclass
class ToolCall:
    text: str
    is_error: bool
    source: str = "live"  # live | cache (fresh hit) | cache-fallback (server unavailable)
    cached_at: str | None = None
    outcome_uncertain: bool = False
    payments: list[dict[str, Any]] = field(default_factory=list)  # QR codes / payment links to show the user


def _render_result(result: Any) -> tuple[str, bool]:
    if not isinstance(result, types.CallToolResult):
        return f"Server returned an unsupported result type: {type(result).__name__}", True
    parts: list[str] = []
    for block in result.content:
        if isinstance(block, types.TextContent):
            parts.append(block.text)
        elif isinstance(block, types.EmbeddedResource):
            res = block.resource
            parts.append(getattr(res, "text", None) or f"[resource {res.uri}]")
        else:
            parts.append(f"[{block.type} content omitted]")
    if not parts and result.structured_content is not None:
        parts.append(json.dumps(result.structured_content, ensure_ascii=False))
    return ("\n".join(parts) or "(empty result)"), bool(result.is_error)
