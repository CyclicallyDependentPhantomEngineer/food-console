"""Log in to / out of Zomato, Swiggy, District, Zepto and Blinkit (mcp-remote OAuth) with one command.

    food-auth login  --app swiggy
    food-auth logout --app zomato
    food-auth status
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

from mcp import StdioServerParameters

from .mcp_bridge import (
    DEFAULT_CONFIG,
    PROVIDERS,
    MCPBridge,
    default_keys,
    auth_files,
    load_server_params,
    logout,
    remote_pids,
    server_url,
    stop_pids,
)

# --app value -> server keys in mcp_config.json
APPS: dict[str, list[str]] = {
    "all": [key for _, key in PROVIDERS["all"].servers],
    "zomato": [key for _, key in PROVIDERS["zomato"].servers],
    "swiggy": [key for _, key in PROVIDERS["swiggy"].servers],
    "district": [key for _, key in PROVIDERS["district"].servers],
    "zepto": [key for _, key in PROVIDERS["zepto"].servers],
    "blinkit": [key for _, key in PROVIDERS["blinkit"].servers],
    **{key: [key] for _, key in PROVIDERS["swiggy"].servers},
}


def _servers(app: str, config: Path) -> list[tuple[str, StdioServerParameters]]:
    keys = default_keys(APPS[app], config) if app == "all" else APPS[app]  # "all" skips disabled servers
    return [(key, load_server_params(key, config)) for key in keys]


def _token_file(params: StdioServerParameters) -> Path | None:
    url = server_url(params)
    if url is None:
        return None
    return next((p for p in auth_files(url) if p.name.endswith("_tokens.json")), None)


# Servers whose login is a local browser session (phone + OTP) instead of mcp-remote OAuth.
LOCAL_SESSIONS = {"blinkit-mcp"}


async def _blinkit_login(force: bool) -> bool:
    from .blinkit import session
    from .blinkit.browser import Blinkit

    if session.has_login(session.load_state()) and not force:
        print("blinkit-mcp: already logged in (use --force to log in again).")
        return True
    if force:
        session.clear()
    bk = Blinkit()
    try:
        print("blinkit-mcp: opening blinkit.com in Chrome ...")
        await bk.start()
        if await bk.is_logged_in():
            await bk.save()
            print("blinkit-mcp: logged in.")
            return True
        phone = (await asyncio.to_thread(input, "Blinkit mobile number (10 digits): ")).strip()
        print(await bk.send_otp(phone))
        otp = (await asyncio.to_thread(input, "OTP from the SMS: ")).strip()
        msg = await bk.verify_otp(otp)
        print(f"blinkit-mcp: {msg}")
        return msg.startswith("Logged in")
    except Exception as exc:
        print(f"blinkit-mcp: login failed: {exc}")
        return False
    finally:
        await bk.close()


def status(servers: list[tuple[str, StdioServerParameters]]) -> int:
    for key, params in servers:
        if key in LOCAL_SESSIONS:
            from .blinkit.session import SESSION_PATH, has_login, load_state

            if has_login(load_state()):
                saved = datetime.fromtimestamp(SESSION_PATH.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                print(f"{key:18} logged in      (session saved {saved}, encrypted)")
            else:
                print(f"{key:18} not logged in")
            continue
        token = _token_file(params)
        url = server_url(params)
        running = remote_pids(url) if url else set()
        extra = f"  [mcp-remote running: PIDs {' '.join(map(str, sorted(running)))}]" if running else ""
        if token:
            saved = datetime.fromtimestamp(token.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            print(f"{key:18} logged in      (token saved {saved}){extra}")
        else:
            print(f"{key:18} not logged in{extra}")
    return 0


async def _login_one(key: str, params: StdioServerParameters, timeout: float) -> bool:
    print(f"{key}: opening browser login (finish it within {int(timeout)} s) ...")
    try:
        async with MCPBridge([(key, params)], connect_timeout=timeout) as bridge:
            print(f"{key}: logged in ({len(bridge.tools)} tools available).")
            return True
    except TimeoutError:
        print(f"{key}: login timed out.")
    except Exception as exc:
        print(f"{key}: login failed: {exc}")
    return False


async def login(
    servers: list[tuple[str, StdioServerParameters]], *, force: bool, timeout: float, kill_stale: bool = False
) -> int:
    failed = 0
    # One at a time: parallel mcp-remote logins compete for the same OAuth callback port.
    for key, params in servers:
        if key in LOCAL_SESSIONS:
            failed += 0 if await _blinkit_login(force) else 1
            continue
        url = server_url(params)
        if _token_file(params) and not force:
            print(f"{key}: already logged in (use --force to log in again).")
            continue
        # mcp-remote waits forever for another instance that is "running the sign-in" for
        # the same server, e.g. one left behind by an interrupted earlier login.
        running = remote_pids(url) if url else set()
        if running and not kill_stale:
            pids = " ".join(map(str, sorted(running)))
            print(f"{key}: another mcp-remote for this server is already running (PIDs {pids}).")
            print("    It may be a stuck earlier login, or food-ui / a chat app that is still open.")
            print(f"    Close that app, or rerun with --kill-stale to stop it (kill {pids}).")
            failed += 1
            continue
        if running:
            print(f"{key}: stopping {len(running)} leftover mcp-remote process(es).")
            stop_pids(running)
        if force and url:
            logout(url)
        if not await _login_one(key, params, timeout):
            failed += 1
    return 1 if failed else 0


def do_logout(servers: list[tuple[str, StdioServerParameters]]) -> int:
    for key, params in servers:
        if key in LOCAL_SESSIONS:
            from .blinkit.session import clear

            print(f"{key}: logged out (saved session deleted)." if clear() else f"{key}: was not logged in.")
            continue
        url = server_url(params)
        if url is None:
            print(f"{key}: not an mcp-remote server; skipped.")
            continue
        removed = logout(url)
        print(f"{key}: logged out ({len(removed)} files removed)." if removed else f"{key}: was not logged in.")
    print("Logout is local only; to revoke access fully, sign out of all sessions in the provider's app.")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(prog="food-auth", description="Log in to / out of Zomato, Swiggy, District, Zepto and Blinkit.")
    p.add_argument("action", choices=["login", "logout", "status"])
    p.add_argument("--app", choices=list(APPS), default="all", help="which app (default: all)")
    p.add_argument("--force", action="store_true", help="login: log in again even if already logged in")
    p.add_argument("--timeout", type=float, default=300, help="login: seconds to finish each browser login")
    p.add_argument(
        "--kill-stale",
        action="store_true",
        help="login: stop mcp-remote processes already running for the server (e.g. a stuck earlier login)",
    )
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = p.parse_args()

    servers = _servers(args.app, args.config)
    if args.action == "status":
        code = status(servers)
    elif args.action == "logout":
        code = do_logout(servers)
    else:
        try:
            code = asyncio.run(login(servers, force=args.force, timeout=args.timeout, kill_stale=args.kill_stale))
        except KeyboardInterrupt:
            print("\nLogin cancelled.")
            code = 130
    sys.exit(code)
