"""Conservative cache identity when providers do not expose a stable account subject.

Only a digest leaves this module. Token refresh also changes identity: restarting and
refetching is preferable to reusing another account's personal data.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


class AccountChanged(RuntimeError):
    def __init__(self):
        super().__init__("Account credentials changed. Private caches were cleared; restart Food Console and sign in again.")


def credential_fingerprint(config_path: Path | None = None) -> str:
    from .mcp_bridge import DEFAULT_CONFIG, auth_dir, auth_files
    from .blinkit.session import SESSION_PATH, load_state

    config_path = config_path or DEFAULT_CONFIG
    config = json.loads(config_path.read_text())
    digest = hashlib.sha256(config_path.resolve().as_posix().encode())
    digest.update(json.dumps(config, sort_keys=True).encode())
    digest.update(str(auth_dir().resolve()).encode())
    for entry in config.get("mcpServers", {}).values():
        for url in entry.get("args", []):
            if not url.startswith(("https://", "http://")):
                continue
            # Explicit per-server credential directories must be included as well.
            custom = entry.get("env", {}).get("MCP_REMOTE_CONFIG_DIR") if entry.get("env") else None
            if custom:
                key = hashlib.md5(url.encode()).hexdigest()
                files = sorted(Path(custom).glob(f"mcp-remote-*/{key}_tokens.json"))
            else:
                files = [p for p in auth_files(url) if p.name.endswith("_tokens.json")]
            digest.update(url.encode())
            for path in files:
                digest.update(str(path.resolve()).encode())
                try:
                    digest.update(path.read_bytes())
                except FileNotFoundError:
                    digest.update(b"missing")
    if SESSION_PATH.exists():
        state = load_state() or {}
        # Location/cart changes rewrite the encrypted file but do not change login.
        auth = [(o.get("origin"), v.get("value")) for o in state.get("origins", [])
                for v in o.get("localStorage", []) if v.get("name") == "auth"]
        digest.update(json.dumps(auth, sort_keys=True).encode())
    return digest.hexdigest()
