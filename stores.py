"""
stores.py — pluggable key/value backends behind one URL convention.

Every persistent store in the package (the mapping cache today, the learned
synonyms next) is a `KeyValueStore`. You pick the backend with a URL, exactly
like SQLAlchemy / Celery — swap it with an env var, no code change:

    memory://                         in-process dict (tests, single worker)
    ./mapping_cache.db  /  sqlite:///mapping_cache.db
                                      SQLite file — no server, concurrency-safe (DEFAULT)
    ./mapping_cache.json / file://... legacy JSON file (NOT multi-worker safe)
    redis://host:6379/0               Redis            (pip install ...[redis])
    postgresql://user@host/db         Postgres         (pip install ...[postgres])

Escape hatch: any object with get()/put() works — pass your own to open_store's
consumers directly if you have a backend we don't ship.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Optional

try:                       # typing only; Protocol may be absent on very old pythons
    from typing import Protocol
except ImportError:        # pragma: no cover
    Protocol = object      # type: ignore


class KeyValueStore(Protocol):
    def get(self, key: str) -> Optional[dict]: ...
    def put(self, key: str, value: dict) -> None: ...
    def close(self) -> None: ...


# --------------------------------------------------------------------------
# In-memory
# --------------------------------------------------------------------------
class MemoryStore:
    def __init__(self) -> None:
        self._d: dict[str, dict] = {}

    def get(self, key: str) -> Optional[dict]:
        return self._d.get(key)

    def put(self, key: str, value: dict) -> None:
        self._d[key] = value

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------
# JSON file (legacy default; whole-file rewrite, NOT multi-worker safe)
# --------------------------------------------------------------------------
class JsonFileStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._data: dict[str, dict] = {}
        self._lock = threading.Lock()
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    self._data = json.load(fh)
            except (json.JSONDecodeError, OSError):
                self._data = {}

    def get(self, key: str) -> Optional[dict]:
        return self._data.get(key)

    def put(self, key: str, value: dict) -> None:
        with self._lock:
            self._data[key] = value
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2)
            os.replace(tmp, self.path)   # atomic-ish within a single process

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------
# SQLite (default) — file-based, no server, concurrency-safe via WAL
# --------------------------------------------------------------------------
class SqliteStore:
    def __init__(self, path: str) -> None:
        import sqlite3
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self._conn.commit()

    def get(self, key: str) -> Optional[dict]:
        cur = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,))
        row = cur.fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, value: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)))
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()


# --------------------------------------------------------------------------
# Redis (optional dep, lazy import)
# --------------------------------------------------------------------------
class RedisStore:
    def __init__(self, url: str, prefix: str = "bankmap:") -> None:
        try:
            import redis
        except ImportError as exc:  # friendly, actionable message
            raise ImportError(
                "The redis cache backend needs the 'redis' package. Install it "
                "with:  pip install bank-statement-mapper[redis]   (or `pip "
                "install redis`). It is optional — the default SQLite backend "
                "needs nothing extra."
            ) from exc
        self._r = redis.Redis.from_url(url)
        self._prefix = prefix

    def get(self, key: str) -> Optional[dict]:
        raw = self._r.get(self._prefix + key)
        return json.loads(raw) if raw else None

    def put(self, key: str, value: dict) -> None:
        self._r.set(self._prefix + key, json.dumps(value))

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------
# Postgres (optional dep, lazy import)
# --------------------------------------------------------------------------
class PostgresStore:
    def __init__(self, url: str, table: str = "bank_mapper_kv") -> None:
        try:
            import psycopg
        except ImportError as exc:
            raise ImportError(
                "The postgres cache backend needs the 'psycopg' package. Install "
                "it with:  pip install bank-statement-mapper[postgres]. It is "
                "optional — the default SQLite backend needs nothing extra."
            ) from exc
        self._table = table
        self._conn = psycopg.connect(url, autocommit=True)
        self._conn.execute(
            f"CREATE TABLE IF NOT EXISTS {table} "
            "(key TEXT PRIMARY KEY, value JSONB NOT NULL)")

    def get(self, key: str) -> Optional[dict]:
        cur = self._conn.execute(
            f"SELECT value FROM {self._table} WHERE key = %s", (key,))
        row = cur.fetchone()
        return row[0] if row else None

    def put(self, key: str, value: dict) -> None:
        self._conn.execute(
            f"INSERT INTO {self._table} (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (key, json.dumps(value)))

    def close(self) -> None:
        self._conn.close()


# --------------------------------------------------------------------------
# The factory
# --------------------------------------------------------------------------
def open_store(url: Optional[str]) -> KeyValueStore:
    """Return a KeyValueStore for a URL/path. `None` -> in-memory."""
    if not url or url == "memory://" or url == "memory:":
        return MemoryStore()
    if url.startswith(("redis://", "rediss://")):
        return RedisStore(url)
    if url.startswith(("postgresql://", "postgres://")):
        return PostgresStore(url)
    if url.startswith("sqlite://"):
        # sqlite:///abs/or/rel.db  ->  strip scheme
        path = url[len("sqlite:///"):] if url.startswith("sqlite:///") else url[len("sqlite://"):]
        return SqliteStore(path or ":memory:")
    if url.startswith("file://"):
        url = url[len("file://"):]
    # bare path: choose by extension
    if url.endswith((".db", ".sqlite", ".sqlite3")):
        return SqliteStore(url)
    return JsonFileStore(url)
