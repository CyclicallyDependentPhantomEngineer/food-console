"""Local SQLite store: cached read-only tool results and tool lists, saved order history,
the last dish pick and the token-usage history.

Used as a fallback when a server is offline or rate-limited (HTTP 429), and to avoid
repeating identical read requests within a short window.

Personal data is encrypted at rest (Fernet: AES-128-CBC + HMAC-SHA256): cached tool
arguments and results, saved orders and their metadata, the last dish pick, and the
token-usage history (which includes the user's messages). The key lives in the macOS
Keychain (any `keyring` backend elsewhere); if no keychain is available it falls back to
an owner-only key file next to the database. Order ids are stored only as keyed (HMAC) hashes.
Tool definitions (public API descriptions) are kept in plain text. The file itself is
owner-only (0600) inside an owner-only (0700) `.cache/` folder.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .credentials import AccountChanged, credential_fingerprint

from cryptography.fernet import Fernet, InvalidToken

DB_PATH = Path(__file__).resolve().parents[2] / ".cache" / "food.db"
KEYRING_SERVICE = "food-console"
# Set FOOD_CONSOLE_KEY_BACKEND=file to skip the keychain (tests, headless machines).
KEY_BACKEND_ENV = "FOOD_CONSOLE_KEY_BACKEND"

SCHEMA_VERSION = 2  # 1: plaintext (before encryption), 2: encrypted
SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_results (
    server      TEXT NOT NULL,
    tool        TEXT NOT NULL,
    args_hash   TEXT NOT NULL,   -- keyed HMAC of the arguments
    args_enc    TEXT NOT NULL,
    result_enc  TEXT NOT NULL,
    fetched_at  REAL NOT NULL,
    PRIMARY KEY (server, tool, args_hash)
);
CREATE TABLE IF NOT EXISTS tool_defs (
    server      TEXT NOT NULL,
    name        TEXT NOT NULL,
    definition  TEXT NOT NULL,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (server, name)
);
CREATE TABLE IF NOT EXISTS orders_enc (
    id_hash     TEXT PRIMARY KEY,  -- keyed HMAC of provider|service|order_id
    data_enc    TEXT NOT NULL,
    saved_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta_enc (
    key         TEXT PRIMARY KEY,
    value_enc   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  REAL NOT NULL,
    data_enc    TEXT NOT NULL      -- the run (incl. your message text), encrypted
);
CREATE TABLE IF NOT EXISTS picks_enc (
    slot        TEXT PRIMARY KEY,  -- "last": the most recent dish pick
    data_enc    TEXT NOT NULL,
    saved_at    REAL NOT NULL
);
"""


def _load_key(db_path: Path) -> bytes:
    """The encryption key for this database: from the keychain, or an owner-only key file."""
    account = str(db_path.resolve())
    if os.environ.get(KEY_BACKEND_ENV) != "file":
        try:
            import keyring
            from keyring.errors import KeyringError, NoKeyringError

            try:
                key = keyring.get_password(KEYRING_SERVICE, account)
                if not key:
                    key = Fernet.generate_key().decode()
                    keyring.set_password(KEYRING_SERVICE, account, key)
                return key.encode()
            except (NoKeyringError, KeyringError) as exc:
                print(f"note: no usable keychain ({exc}); storing the cache key in a file", file=sys.stderr)
        except ImportError:
            pass
    key_file = db_path.with_suffix(db_path.suffix + ".key")
    if key_file.exists():
        return key_file.read_bytes().strip()
    key = Fernet.generate_key()
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    return key


def forget_key(db_path: Path | None = None) -> None:
    """Remove this database's key from the keychain / key file (used by `food-cache clear all --forget-key`)."""
    path = Path(db_path or DB_PATH)
    try:
        import keyring

        keyring.delete_password(KEYRING_SERVICE, str(path.resolve()))
    except Exception:
        pass
    path.with_suffix(path.suffix + ".key").unlink(missing_ok=True)


@dataclass
class CachedResult:
    text: str
    fetched_at: float

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.fetched_at)

    @property
    def fetched_iso(self) -> str:
        return datetime.fromtimestamp(self.fetched_at, timezone.utc).astimezone().isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path | None = None, key: bytes | None = None, *,
                 credential_source: Callable[[], str] | None = None):
        self.path = Path(path or DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)  # .cache holds personal data: owner-only
        fresh = not self.path.exists()
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        if fresh:
            os.chmod(self.path, 0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self._key = key or _load_key(self.path)
        self._fernet = Fernet(self._key)
        # Separate HMAC key derived from the encryption key, for lookup hashes.
        self._mac_key = hashlib.sha256(b"food-console/lookup/" + self._key).digest()
        self._upgrade()
        # Unscoped legacy exports cannot be attributed to the current account.
        self.path.with_name("orders.json").unlink(missing_ok=True)
        self._credential_source = credential_source or credential_fingerprint
        self._identity = self._mac(self._credential_source())
        self._stale_identity = False
        self.db.execute("CREATE TABLE IF NOT EXISTS credential_scope (slot INTEGER PRIMARY KEY, identity TEXT NOT NULL)")
        self._bind_identity(self._identity)

    def _bind_identity(self, identity: str) -> None:
        # Atomically discard every private table on the first secure startup or a
        # credential/config change. Public tool schemas can be rediscovered safely.
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT identity FROM credential_scope WHERE slot=1").fetchone()
            if row is None or row[0] != identity:
                for table in ("tool_results", "orders_enc", "meta_enc", "picks_enc", "usage_runs", "tool_defs"):
                    self.db.execute(f"DELETE FROM {table}")
                self.db.execute("INSERT OR REPLACE INTO credential_scope VALUES (1, ?)", (identity,))

    def ensure_identity(self) -> None:
        current = self._mac(self._credential_source())
        row = self.db.execute("SELECT identity FROM credential_scope WHERE slot=1").fetchone()
        if current != self._identity or not row or row[0] != self._identity:
            self._bind_identity(current)
            self._stale_identity = True
        if self._stale_identity:
            raise AccountChanged()

    def close(self) -> None:
        self.db.close()

    # ---- crypto helpers ----
    def _enc(self, text: str) -> str:
        return self._fernet.encrypt(text.encode()).decode()

    def _dec(self, token: str) -> str:
        return self._fernet.decrypt(token.encode()).decode()

    def _mac(self, text: str) -> str:
        return hmac.new(self._mac_key, text.encode(), hashlib.sha256).hexdigest()

    def args_hash(self, args: dict[str, Any]) -> str:
        return self._mac(json.dumps(args or {}, sort_keys=True, ensure_ascii=False))

    # ---- tool results ----
    def get_result(self, server: str, tool: str, args: dict[str, Any]) -> CachedResult | None:
        self.ensure_identity()
        row = self.db.execute(
            "SELECT result_enc, fetched_at FROM tool_results WHERE server=? AND tool=? AND args_hash=?",
            (server, tool, self.args_hash(args)),
        ).fetchone()
        if not row:
            return None
        try:
            return CachedResult(self._dec(row[0]), row[1])
        except InvalidToken:  # key changed/lost: treat as a cache miss
            return None

    def put_result(self, server: str, tool: str, args: dict[str, Any], result: str) -> None:
        self.ensure_identity()
        self.db.execute(
            "INSERT OR REPLACE INTO tool_results VALUES (?, ?, ?, ?, ?, ?)",
            (server, tool, self.args_hash(args), self._enc(json.dumps(args or {}, sort_keys=True, ensure_ascii=False)),
             self._enc(result), time.time()),
        )

    def invalidate(self, server: str) -> int:
        """Drop a server's cached results (after something on it changed, e.g. the cart)."""
        self.ensure_identity()
        return self.db.execute("DELETE FROM tool_results WHERE server=?", (server,)).rowcount

    # ---- tool definitions (public API descriptions, not personal) ----
    def save_tool_defs(self, server: str, defs: list[dict[str, Any]]) -> None:
        self.ensure_identity()
        now = time.time()
        with self.db:
            self.db.execute("BEGIN")
            self.db.execute("DELETE FROM tool_defs WHERE server=?", (server,))
            self.db.executemany(
                "INSERT INTO tool_defs VALUES (?, ?, ?, ?)",
                [(server, d["name"], json.dumps(d, ensure_ascii=False), now) for d in defs],
            )

    def load_tool_defs(self, server: str) -> list[dict[str, Any]]:
        self.ensure_identity()
        rows = self.db.execute("SELECT definition FROM tool_defs WHERE server=? ORDER BY name", (server,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    # ---- orders ----
    def _order_id_hash(self, o: dict[str, Any], i: int) -> str:
        oid = str(o.get("order_id") or f"_noid_{i}_{o.get('placed_at', '')}")
        return self._mac(f"{o.get('provider', '')}|{o.get('service', '')}|{oid}")

    def save_orders(self, data: dict[str, Any]) -> None:
        """Replace the saved order set with `data["orders"]` (already merged by the caller)."""
        self.ensure_identity()
        now = time.time()
        rows = [(self._order_id_hash(o, i), self._enc(json.dumps(o, ensure_ascii=False)), now)
                for i, o in enumerate(data.get("orders", []))]
        with self.db:
            self.db.execute("BEGIN")
            self.db.execute("DELETE FROM orders_enc")
            self.db.executemany("INSERT OR REPLACE INTO orders_enc VALUES (?, ?, ?)", rows)
            for key in ("fetched_at", "notes", "added"):
                if key in data:
                    self.db.execute("INSERT OR REPLACE INTO meta_enc VALUES (?, ?)", (key, self._enc(json.dumps(data[key]))))

    def load_orders(self) -> dict[str, Any] | None:
        self.ensure_identity()
        try:
            orders = [json.loads(self._dec(r[0])) for r in self.db.execute("SELECT data_enc FROM orders_enc")]
            meta = {k: json.loads(self._dec(v)) for k, v in self.db.execute("SELECT key, value_enc FROM meta_enc")}
        except InvalidToken:
            print("warning: saved orders can't be decrypted (cache key changed or missing); "
                  "run `food-cache clear orders` and refresh", file=sys.stderr)
            return None
        if not orders and "fetched_at" not in meta:
            return None
        orders.sort(key=lambda o: o.get("placed_at") or "", reverse=True)
        return {"orders": orders, **meta}

    def save_pick(self, data: dict[str, Any]) -> None:
        """Keep the most recent dish-picker result (it includes restaurants near the user)."""
        self.ensure_identity()
        self.db.execute("INSERT OR REPLACE INTO picks_enc VALUES ('last', ?, ?)",
                        (self._enc(json.dumps(data, ensure_ascii=False)), time.time()))

    def save_usage(self, run: dict[str, Any]) -> None:
        """Record one run's token usage (see usage.py)."""
        self.ensure_identity()
        self.db.execute("INSERT INTO usage_runs (started_at, data_enc) VALUES (?, ?)",
                        (run.get("started_at") or time.time(), self._enc(json.dumps(run, ensure_ascii=False))))

    def load_usage(self, since: float | None = None, until: float | None = None, limit: int = 2000) -> list[dict[str, Any]]:
        self.ensure_identity()
        rows = self.db.execute(
            "SELECT id, data_enc FROM usage_runs WHERE started_at >= ? AND started_at < ? ORDER BY started_at DESC LIMIT ?",
            (since or 0, until or 1e12, limit)).fetchall()
        out = []
        for rid, enc in rows:
            try:
                out.append({"id": rid, **json.loads(self._dec(enc))})
            except InvalidToken:
                continue
        return out

    def load_pick(self) -> dict[str, Any] | None:
        self.ensure_identity()
        row = self.db.execute("SELECT data_enc FROM picks_enc WHERE slot='last'").fetchone()
        if not row:
            return None
        try:
            return json.loads(self._dec(row[0]))
        except InvalidToken:
            return None

    # ---- housekeeping ----
    def info(self) -> dict[str, Any]:
        q = lambda sql: self.db.execute(sql).fetchall()  # noqa: E731
        orders: dict[str, int] = {}
        for o in (self.load_orders() or {}).get("orders", []):
            k = f"{o.get('provider')}:{o.get('service')}"
            orders[k] = orders.get(k, 0) + 1
        return {
            "path": str(self.path),
            "size_bytes": self.path.stat().st_size if self.path.exists() else 0,
            "results": {s: {"entries": n, "newest": t} for s, n, t in q(
                "SELECT server, COUNT(*), MAX(fetched_at) FROM tool_results GROUP BY server")},
            "tool_defs": dict(q("SELECT server, COUNT(*) FROM tool_defs GROUP BY server")),
            "orders": orders,
        }

    def clear(self, what: str = "results", server: str | None = None) -> int:
        tables = {"results": ["tool_results"], "tools": ["tool_defs"], "orders": ["orders_enc", "meta_enc"],
                  "picks": ["picks_enc"], "usage": ["usage_runs"],
                  "all": ["tool_results", "tool_defs", "orders_enc", "meta_enc", "picks_enc", "usage_runs"]}[what]
        n = 0
        with self.db:
            self.db.execute("BEGIN")
            for t in tables:  # table names come from the fixed dict above
                if server and t in ("tool_results", "tool_defs"):
                    n += self.db.execute(f"DELETE FROM {t} WHERE server=?", (server,)).rowcount  # noqa: S608
                elif not server:
                    n += self.db.execute(f"DELETE FROM {t}").rowcount  # noqa: S608
        if not server:
            self.db.execute("VACUUM")  # don't leave deleted plaintext/ciphertext pages behind
        return n

    # ---- migrations ----
    def _upgrade(self) -> None:
        """Create tables; convert a plaintext (version 1) database to the encrypted layout."""
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        old_results = "tool_results" in tables and "args_json" in {
            r[1] for r in self.db.execute("PRAGMA table_info(tool_results)")}
        if old_results:
            self.db.execute("ALTER TABLE tool_results RENAME TO tool_results_v1")
        self.db.executescript(SCHEMA)
        if version >= SCHEMA_VERSION and not old_results:
            return
        with self.db:
            self.db.execute("BEGIN")
            if old_results:
                for server, tool, args_json, result, fetched_at in self.db.execute(
                        "SELECT server, tool, args_json, result, fetched_at FROM tool_results_v1").fetchall():
                    args = json.loads(args_json)
                    self.db.execute("INSERT OR REPLACE INTO tool_results VALUES (?, ?, ?, ?, ?, ?)",
                                    (server, tool, self.args_hash(args), self._enc(args_json), self._enc(result), fetched_at))
                self.db.execute("DROP TABLE tool_results_v1")
            if "orders" in tables:
                rows = [json.loads(r[0]) for r in self.db.execute("SELECT data FROM orders")]
                now = time.time()
                for i, o in enumerate(rows):
                    self.db.execute("INSERT OR REPLACE INTO orders_enc VALUES (?, ?, ?)",
                                    (self._order_id_hash(o, i), self._enc(json.dumps(o, ensure_ascii=False)), now))
                self.db.execute("DROP TABLE orders")
            if "meta" in tables:
                for k, v in self.db.execute("SELECT key, value FROM meta").fetchall():
                    self.db.execute("INSERT OR REPLACE INTO meta_enc VALUES (?, ?)", (k, self._enc(v)))
                self.db.execute("DROP TABLE meta")
            self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        if old_results or "orders" in tables or "meta" in tables:
            self.db.execute("VACUUM")  # rewrite the file so the old plaintext pages are gone

def clear_private_cache() -> None:
    """Logout clears persisted personal data even before the next application read."""
    if DB_PATH.exists():
        s = Store()
        try:
            s.clear("all")
        finally:
            s.close()


def main() -> None:
    """`food-cache info` / `food-cache clear [results|tools|orders|picks|usage|all] [--server NAME]`."""
    import argparse

    p = argparse.ArgumentParser(prog="food-cache", description="Inspect or clear the local encrypted cache.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info", help="show what is cached")
    c = sub.add_parser("clear", help="delete cached data")
    c.add_argument("what", nargs="?", default="results", choices=["results", "tools", "orders", "picks", "usage", "all"],
                   help="results (default): cached tool responses; tools: cached tool lists; "
                        "orders: saved order history used by Stats; picks: last dish pick; usage: token usage history; all: everything")
    c.add_argument("--server", help="only this server label (zomato, food, instamart, dineout, district, zepto, blinkit); results/tools only")
    c.add_argument("--forget-key", action="store_true",
                   help="with `all`: also delete the database file and its key from the keychain")
    args = p.parse_args()

    s = Store()
    try:
        if args.cmd == "info":
            i = s.info()
            print(f"{i['path']}  ({i['size_bytes'] / 1024:.0f} KB, encrypted)")
            print("cached tool results:")
            for server, v in sorted(i["results"].items()):
                newest = datetime.fromtimestamp(v["newest"]).strftime("%Y-%m-%d %H:%M")
                print(f"  {server:10} {v['entries']:4} entries, newest {newest}")
            if not i["results"]:
                print("  (none)")
            print("cached tool lists: " + (", ".join(f"{k} ({n})" for k, n in sorted(i["tool_defs"].items())) or "(none)"))
            print("saved orders:      " + (", ".join(f"{k} ({n})" for k, n in sorted(i["orders"].items())) or "(none)"))
        else:
            n = s.clear(args.what, args.server)
            print(f"deleted {n} row(s) ({args.what}{' for ' + args.server if args.server else ''})")
            if args.forget_key and args.what == "all" and not args.server:
                s.close()
                for suffix in ("", "-wal", "-shm"):
                    Path(str(s.path) + suffix).unlink(missing_ok=True)
                forget_key(s.path)
                print("deleted the database and its encryption key")
    finally:
        try:
            s.close()
        except sqlite3.ProgrammingError:
            pass
