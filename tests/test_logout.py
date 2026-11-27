"""Logout deletes only the cached files for the configured servers."""
import hashlib

from food_console.mcp_bridge import PROVIDERS, load_server_params, logout, server_url

ZOMATO = "https://mcp-server.zomato.com/mcp"


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def test_logout_removes_only_matching_files(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_REMOTE_CONFIG_DIR", str(tmp_path))
    cache = tmp_path / "mcp-remote-v1"
    cache.mkdir()
    mine = [cache / f"{_md5(ZOMATO)}_tokens.json", cache / f"{_md5(ZOMATO)}_client_info.json"]
    other = cache / f"{_md5('https://mcp.swiggy.com/food')}_tokens.json"
    for p in [*mine, other]:
        p.write_text("{}")

    assert sorted(logout(ZOMATO)) == sorted(mine)
    assert not any(p.exists() for p in mine)
    assert other.exists()
    assert logout(ZOMATO) == []  # second logout is a no-op


def test_provider_server_urls():
    urls = {key: server_url(load_server_params(key)) for p in PROVIDERS.values() for _, key in p.servers}
    assert urls == {
        "zomato-mcp": ZOMATO,
        "swiggy-food": "https://mcp.swiggy.com/food",
        "swiggy-instamart": "https://mcp.swiggy.com/im",
        "swiggy-dineout": "https://mcp.swiggy.com/dineout",
        "district-mcp": "https://mcp-server.district.in/v2/mcp",
        "zepto-mcp": "https://mcp.zepto.co.in/mcp",
        "blinkit-mcp": None,  # local server, no remote URL
    }
