"""food-auth: app selection, status, logout, and login skipping servers already logged in."""
import asyncio
import hashlib

from food_console import auth


def _seed(tmp_path, url):
    cache = tmp_path / "mcp-remote-v1"
    cache.mkdir(exist_ok=True)
    h = hashlib.md5(url.encode()).hexdigest()
    for suffix in ("_tokens.json", "_client_info.json"):
        (cache / f"{h}{suffix}").write_text("{}")


def test_apps_map_to_servers():
    assert auth.APPS["zomato"] == ["zomato-mcp"]
    assert auth.APPS["swiggy"] == ["swiggy-food", "swiggy-instamart", "swiggy-dineout"]
    assert auth.APPS["swiggy-instamart"] == ["swiggy-instamart"]
    assert auth.APPS["district"] == ["district-mcp"]
    assert auth.APPS["zepto"] == ["zepto-mcp"]
    assert len(auth.APPS["all"]) == 7


def test_status_logout_and_login_skip(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MCP_REMOTE_CONFIG_DIR", str(tmp_path))
    # Ignore any real mcp-remote processes on this machine.
    monkeypatch.setattr(auth, "remote_pids", lambda url: set())
    monkeypatch.setattr("food_console.mcp_bridge.remote_pids", lambda url: set())
    _seed(tmp_path, "https://mcp.swiggy.com/food")
    servers = auth._servers("all", auth.DEFAULT_CONFIG)

    auth.status(servers)
    out = capsys.readouterr().out
    assert "swiggy-food        logged in" in out and "zomato-mcp         not logged in" in out

    # Already logged in: login must not try to connect.
    called = []

    async def fake_login_one(key, params, timeout):
        called.append(key)
        return True

    monkeypatch.setattr(auth, "_login_one", fake_login_one)
    assert asyncio.run(auth.login(auth._servers("swiggy-food", auth.DEFAULT_CONFIG), force=False, timeout=1)) == 0
    assert called == []
    # --force logs out first, then logs in again.
    asyncio.run(auth.login(auth._servers("swiggy-food", auth.DEFAULT_CONFIG), force=True, timeout=1))
    assert called == ["swiggy-food"]

    _seed(tmp_path, "https://mcp.swiggy.com/food")
    auth.do_logout(auth._servers("swiggy", auth.DEFAULT_CONFIG))
    assert not any(tmp_path.rglob("*_tokens.json"))


def _fake_remote(url):
    """A process whose command line looks like a running `mcp-remote <url>`."""
    import subprocess, sys
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "mcp-remote", url])


def _wait_gone(pid, secs=5):
    import time
    from food_console.mcp_bridge import _alive
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if not _alive(pid):
            return True
        time.sleep(0.1)
    return False


def test_remote_pids_and_stop():
    from food_console.mcp_bridge import remote_pids, stop_pids
    p = _fake_remote("https://stale.test/mcp")
    try:
        assert _wait_for(lambda: p.pid in remote_pids("https://stale.test/mcp"))
        assert p.pid not in remote_pids("https://stale.test/mc")  # no prefix matches
        stop_pids({p.pid})
        assert _wait_gone(p.pid)
    finally:
        p.kill()


def _wait_for(cond, secs=5):
    import time
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


def test_login_refuses_when_stale_running_unless_kill_stale(monkeypatch, tmp_path, capsys):
    from mcp import StdioServerParameters
    monkeypatch.setenv("MCP_REMOTE_CONFIG_DIR", str(tmp_path))
    url = "https://stuck.test/mcp"
    servers = [("stuck", StdioServerParameters(command="npx", args=["mcp-remote", url]))]
    called = []

    async def fake_login_one(key, params, timeout):
        called.append(key)
        return True

    monkeypatch.setattr(auth, "_login_one", fake_login_one)
    p = _fake_remote(url)
    try:
        from food_console.mcp_bridge import remote_pids
        assert _wait_for(lambda: p.pid in remote_pids(url))
        assert asyncio.run(auth.login(servers, force=False, timeout=1)) == 1
        assert called == [] and "already running" in capsys.readouterr().out
        assert asyncio.run(auth.login(servers, force=False, timeout=1, kill_stale=True)) == 0
        assert called == ["stuck"] and _wait_gone(p.pid)
    finally:
        p.kill()


def test_bridge_stops_orphan_left_by_failed_connect():
    """The original bug: an interrupted login leaves mcp-remote's node child running."""
    import sys
    from mcp import StdioServerParameters
    from food_console.mcp_bridge import MCPBridge, remote_pids
    url = "https://orphan.test/mcp"
    # Server command that spawns a detached 'mcp-remote <url>' grandchild, then dies.
    spawn = (
        "import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)', "
        f"'mcp-remote', {url!r}], start_new_session=True); sys.exit(1)"
    )
    params = StdioServerParameters(command=sys.executable, args=["-c", spawn, "mcp-remote", url])
    bad = StdioServerParameters(command=sys.executable, args=["-c", "import sys; sys.exit(1)"])

    async def run():
        async with MCPBridge([("orphan", params), ("bad", bad)], connect_timeout=10):
            pass

    try:
        asyncio.run(run())
    except RuntimeError:
        pass  # no server connected - expected
    assert _wait_for(lambda: not remote_pids(url)), "orphaned mcp-remote was not stopped"
