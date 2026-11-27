"""Chat with your Zomato, Swiggy, District, Zepto or Blinkit account through Claude and their MCP servers."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from .agent import MODEL, OrderAgent, ask_yes_no
from .router import Router
from .store import Store
from .credentials import AccountChanged, credential_fingerprint
from .mcp_bridge import (
    DEFAULT_CONFIG,
    PROVIDERS,
    BridgedTool,
    MCPBridge,
    default_keys,
    load_blocked_tools,
    load_read_only_tools,
    load_server_params,
    logout,
    server_url,
)

HELP = "Commands: /tools  /usage  /reset  /quit"


async def _approve(tool: BridgedTool, args: dict) -> bool:
    print("\n" + "=" * 60)
    print(f"APPROVAL NEEDED: {tool.label}")
    print(json.dumps(args, indent=2, ensure_ascii=False))
    print("=" * 60)
    return await ask_yes_no("Run this? [y/N] ")


def _print_tools(mcp: MCPBridge, agent: OrderAgent) -> None:
    for t in mcp.tools.values():
        flag = ""
        if agent.needs_approval(t):
            flag = "  [needs approval]"
        elif t.read_only:
            flag = "  [read-only]"
        desc = (t.tool.description or "").strip().splitlines()[0:1]
        print(f"- {t.label}{flag}\n    {desc[0][:110] if desc else ''}")


def _logout(servers: list[tuple[str, object]]) -> int:
    for label, params in servers:
        url = server_url(params)
        if url is None:
            print(f"{label}: not an mcp-remote server; skipped.")
            continue
        removed = logout(url)
        if not removed:
            print(f"{label}: not logged in ({url}).")
            continue
        for path in removed:
            print(f"removed {path}")
        print(f"{label}: logged out of {url} on this machine.")
    return 0


async def _pick(mcp: MCPBridge, agent: OrderAgent, args: argparse.Namespace, brand: str) -> int:
    from .nutrition import GOALS, pick_dishes, pick_tool_filter, resolve_goal

    params = {"goal": args.goal, "sort": args.sort, "diet": args.diet}
    for k in ("max_kcal", "min_protein", "max_price"):
        if getattr(args, k) is not None:
            params[k] = getattr(args, k)
    try:
        goal = resolve_goal(params)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    delivery = [s for s in mcp.sessions if s in ("zomato", "food")]  # food-delivery servers only
    if not delivery:
        print("error: pick needs a food-delivery server (Zomato or Swiggy Food)", file=sys.stderr)
        return 2
    agent.tool_filter = pick_tool_filter(delivery)
    agent.allow_orders = False
    print(f"Goal: {GOALS.get(args.goal, {}).get('label', 'custom')}. Searching menus ...")
    result = await pick_dishes(agent, goal, query=" ".join(args.query), platforms=brand, address=args.address or "")
    dishes = result["dishes"][: args.top]
    if not dishes:
        print("No dishes found." + (f" Notes: {result['notes']}" if result["notes"] else ""))
        return 1
    print(f"\n{result['fits']} of {len(result['dishes'])} dishes fit"
          + (f" (address: {result['address_label']})" if result["address_label"] else "") + ".\n")
    for d in dishes:
        mark = "OK " if d["fits"] else "-- "
        src = "listed" if d["source"] == "listed" else "est."
        km = f"  {d['distance_km']:g} km away" if d.get("distance_km") is not None else ""
        print(f"{mark}{d['rank']:>2}. {d['dish']} - {d['merchant']} ({d['provider']})  Rs {d['price_inr']:.0f}{km}")
        print(f"      {d['kcal']} kcal  {d['protein_g']:.0f} g protein ({src})  "
              f"{d['protein_per_100kcal']} g/100 kcal  {d['protein_per_100inr']} g/Rs 100")
        if d["listed_issues"]:
            print(f"      menu says {d['listed']['kcal']:.0f} kcal but {d['listed_issues'][0]}; using the estimate")
        if d["caution"]:
            print(f"      note: {d['caution']}")
        failed = [c["detail"] for c in d["checks"] if not c["ok"]]
        if failed:
            print(f"      misses: {', '.join(failed)}")
    if result["notes"]:
        print(f"\nNotes: {result['notes']}")
    print("\n'est.' = Claude's estimate for one serving; 'listed' = the platform's own numbers.")
    return 0


async def _main(args: argparse.Namespace) -> int:
    provider = PROVIDERS[args.provider]
    keys = args.server or [key for _, key in provider.servers]
    if not args.server and len(provider.servers) > 1 and args.provider == "all":
        keys = default_keys(keys, args.config)  # "all" leaves out servers disabled in the config
    labels = {key: label for label, key in provider.servers}
    servers = [(labels.get(key, key), load_server_params(key, args.config)) for key in keys]
    read_only_tools = {labels.get(key, key): load_read_only_tools(key, args.config) for key in keys}
    blocked_tools = {labels.get(key, key): load_blocked_tools(key, args.config) for key in keys}
    entries = json.loads(args.config.read_text())["mcpServers"]
    operations = {labels.get(key, key): entries[key].get("readOnlyOperations", {}) for key in keys}
    payments = {labels.get(key, key): entries[key].get("paymentTools", {}) for key in keys}

    if args.command == "logout":
        return _logout(servers)

    print(f"Connecting to {provider.brand}: {', '.join(label for label, _ in servers)} ...")
    print("First run opens a browser login per server; mcp-remote caches tokens in ~/.mcp-auth.")

    store = Store(credential_source=lambda: credential_fingerprint(args.config))
    async with MCPBridge(servers, store=store, read_only_tools=read_only_tools,
                         blocked_tools=blocked_tools, read_only_operations=operations, payment_tools=payments) as mcp:
        agent = OrderAgent(
            mcp,
            _approve,
            provider=provider,
            allow_orders=not args.no_orders,
            verbose=args.verbose,
            profile=args.profile,
        )
        agent.usage_kind, agent.on_usage = f"cli:{args.command or 'chat'}", store.save_usage
        agent.router, agent.model_override = Router.from_env(), args.model
        print(f"Connected to {', '.join(mcp.sessions)}. {len(mcp.tools)} tools. "
              f"Model: {'auto routing' if args.model == 'auto' and agent.router.enabled else args.model}")

        if args.command == "tools":
            _print_tools(mcp, agent)
            return 0

        if args.command == "pick":
            return await _pick(mcp, agent, args, provider.brand)

        if args.command == "ask":
            print(await agent.send(" ".join(args.prompt)))
            return 0

        print(HELP)
        while True:
            try:
                user = (await asyncio.to_thread(input, "\nyou> ")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not user:
                continue
            if user in {"/quit", "/exit"}:
                return 0
            if user == "/tools":
                _print_tools(mcp, agent)
                continue
            if user == "/usage":
                print(agent.usage)
                continue
            if user == "/reset":
                agent.messages.clear()
                agent.mutation_uncertain = False
                print("Conversation cleared.")
                continue
            print("\nclaude> " + await agent.send(user))


def _use_ant_profile() -> None:
    """Make sure the `ant auth login` profile is what authenticates Claude calls.

    ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN outrank the profile, and even an
    empty value claims that slot, so remove them from this process.
    """
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        if var in os.environ:
            if os.environ[var]:
                print(f"note: ignoring {var}; using the `ant auth login` profile instead", file=sys.stderr)
            del os.environ[var]


def _run(default_provider: str) -> None:
    load_dotenv()
    _use_ant_profile()
    p = argparse.ArgumentParser(prog=f"{default_provider}-chat", description=__doc__)
    p.add_argument("--provider", choices=sorted(PROVIDERS), default=default_provider)
    p.add_argument("--profile", help="ant profile to use (default: active profile or ANTHROPIC_PROFILE)")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="MCP config JSON (default: mcp_config.json)")
    p.add_argument(
        "--server",
        action="append",
        help="server key in mcpServers to use instead of the provider's set (repeatable), e.g. swiggy-instamart",
    )
    p.add_argument("--model", default="auto", choices=["auto", "haiku", "sonnet", "opus"],
                   help="auto (default): route each request to Haiku, Sonnet or Opus; or force one model")
    p.add_argument("--no-orders", action="store_true", help="block order/booking/payment tools (analysis only)")
    p.add_argument("-v", "--verbose", action="store_true", help="print tool arguments")
    sub = p.add_subparsers(dest="command")
    sub.add_parser("chat", help="interactive chat (default)")
    sub.add_parser("tools", help="list the MCP tools and exit")
    sub.add_parser("logout", help="delete the cached login tokens for the provider's servers on this machine")
    pick = sub.add_parser("pick", help="suggest what to order by calories and protein")
    pick.add_argument("query", nargs="*", help="what you feel like, e.g. chicken, south indian (optional)")
    pick.add_argument("--goal", default="lean", choices=["lean", "protein", "light", "value", "treat", "custom"],
                      help="preset: lean (<=650 kcal, >=30 g), protein (>=40 g), light (<=450 kcal, >=15 g), "
                           "value (<=Rs 300, >=25 g), treat (no calorie/protein limits); explicit limits below override it")
    pick.add_argument("--max-kcal", type=int)
    pick.add_argument("--min-protein", type=int)
    pick.add_argument("--max-price", type=int)
    pick.add_argument("--diet", default="any", choices=["any", "veg", "non_veg"])
    pick.add_argument("--sort", choices=["density", "protein", "kcal", "per_rupee", "rating", "indulgent"])
    pick.add_argument("--address", help="saved address label to deliver to (default: the first)")
    pick.add_argument("--top", type=int, default=10, help="how many to show (default 10)")
    ask = sub.add_parser("ask", help="one-shot question, e.g. analysis")
    ask.add_argument("prompt", nargs="+")
    args = p.parse_args()
    sys.exit(asyncio.run(_main(args)))


def main() -> None:
    _run("zomato")


def swiggy_main() -> None:
    _run("swiggy")


def district_main() -> None:
    _run("district")


def zepto_main() -> None:
    _run("zepto")


def blinkit_main() -> None:
    _run("blinkit")
