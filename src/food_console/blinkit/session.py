"""Blinkit browser session (cookies + local storage), stored encrypted next to the cache.

The original project kept this as plaintext JSON under ~/.blinkit_mcp with default file
permissions; it contains the Blinkit access token, so here it is Fernet-encrypted with the
same key as the rest of the local cache (macOS Keychain) and written owner-only.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from ..store import DB_PATH, _load_key

SESSION_PATH = DB_PATH.with_name("blinkit_session.enc")


def _fernet(path: Path) -> Fernet:
    return Fernet(_load_key(path.with_name(DB_PATH.name)))


def save_state(state: dict[str, Any], path: Path = SESSION_PATH) -> None:
    if path == SESSION_PATH and path.exists():
        previous, current = _phone(load_state(path)), _phone(state)
        if previous and current and previous != current:
            from ..store import clear_private_cache
            clear_private_cache()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    token = _fernet(path).encrypt(json.dumps(state).encode())
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(token)
    os.replace(tmp, path)


def _phone(state: dict[str, Any] | None) -> str | None:
    """Stable login identity; access-token and cart refreshes are deliberately ignored."""
    for origin in (state or {}).get("origins", []):
        if origin.get("origin") != "https://blinkit.com":
            continue
        for item in origin.get("localStorage", []):
            if item.get("name") == "auth":
                try:
                    phone = json.loads(item.get("value") or "{}").get("phoneNumber")
                    if isinstance(phone, str) and phone:
                        return phone
                except (ValueError, AttributeError):
                    pass
    return None


def load_state(path: Path = SESSION_PATH) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(_fernet(path).decrypt(path.read_bytes()))
    except (InvalidToken, ValueError):
        return None  # key changed or file damaged: behave as logged out


def has_login(state: dict[str, Any] | None) -> bool:
    """True if the saved browser state holds a Blinkit login (localStorage auth token/phone)."""
    for origin in (state or {}).get("origins", []):
        if "blinkit.com" not in origin.get("origin", ""):
            continue
        for item in origin.get("localStorage", []):
            if item.get("name") == "auth":
                try:
                    auth = json.loads(item.get("value") or "{}")
                except ValueError:
                    continue
                if auth.get("accessToken") or auth.get("phoneNumber"):
                    return True
    return False


def clear(path: Path = SESSION_PATH) -> bool:
    if path.exists():
        path.unlink()
        if path == SESSION_PATH:
            from ..store import clear_private_cache
            clear_private_cache()
        return True
    return False
