"""Local account generations, independent of token refresh and configuration edits.

Stable OAuth subjects are cache-partition hints, never authentication grants. Opaque
credentials cannot identify an account: use food-auth logout/login --force to switch
those accounts. A new service joins the current profile without discarding history.
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import uuid
from pathlib import Path


class AccountChanged(RuntimeError):
    def __init__(self):
        super().__init__("Account changed. Previous history was archived separately; restart Food Console and sign in again.")


def _subject(tokens: dict) -> str | None:
    if not isinstance(tokens, dict):
        return None
    for name in ("id_token", "access_token"):
        token = tokens.get(name)
        if not isinstance(token, str) or len(token) > 64_000:
            continue
        try:
            body = token.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
            if isinstance(claims.get("sub"), str) and claims["sub"]:
                return hashlib.sha256(json.dumps([claims.get("iss"), claims["sub"]]).encode()).hexdigest()
        except (ValueError, IndexError, TypeError, AttributeError):
            pass
    return None


def credential_fingerprint(config_path: Path | None = None, *, rotate: bool = False) -> str:
    from .mcp_bridge import auth_dir
    from .store import DB_PATH

    # Only a running client with an explicit config observes provider identities.
    # Cache inspection/logout must not re-interpret a custom client's config using
    # the default one and accidentally switch its account generation back.
    config = json.loads(config_path.read_text()) if config_path is not None else {}
    # This file contains random generations and hashes only, never credentials.
    path = DB_PATH.with_name("account-scope.json")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+") as file:
        fcntl.flock(file, fcntl.LOCK_EX)
        content = file.read()
        state = json.loads(content) if content else {"generation": uuid.uuid4().hex, "services": {}}
        changed = rotate
        for name, entry in config.get("mcpServers", {}).items():
            for url in entry.get("args", []):
                if not url.startswith(("https://", "http://")):
                    continue
                directory = Path((entry.get("env") or {}).get("MCP_REMOTE_CONFIG_DIR") or auth_dir()).expanduser()
                location = hashlib.sha256(f"{url}|{directory.resolve()}".encode()).hexdigest()
                key = hashlib.md5(url.encode()).hexdigest()
                subject = None
                # Old connector versions may leave token files behind. Prefer the
                # newest readable file; token contents/timestamps never form identity.
                files = []
                for token_file in directory.glob(f"mcp-remote-*/{key}_tokens.json"):
                    try:
                        files.append((token_file.stat().st_mtime_ns, token_file))
                    except FileNotFoundError:
                        pass
                for _, token_file in sorted(files, reverse=True):
                    try:
                        candidate = _subject(json.loads(token_file.read_text()))
                    except (OSError, ValueError):
                        continue  # a refresh may be replacing the file
                    subject = candidate
                    break
                service = hashlib.sha256(name.encode()).hexdigest()
                previous = state["services"].get(service)
                if previous:
                    changed |= previous["location"] != location
                    changed |= bool(subject and previous.get("subject") and subject != previous["subject"])
                state["services"][service] = {"location": location,
                    "subject": subject or (previous or {}).get("subject")}
        if changed:
            state["generation"] = uuid.uuid4().hex
        updated = json.dumps(state, sort_keys=True)
        if updated != content:
            file.seek(0)
            file.write(updated)
            file.truncate()
            file.flush()
            os.fsync(file.fileno())
        return state["generation"]
