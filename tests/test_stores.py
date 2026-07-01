"""
Tests for the pluggable store layer (stores.open_store) and the SQLite-backed
MappingCache. Redis/Postgres adapters need a live server, so they're not
exercised here — only the URL routing to them is checked.
"""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bank_statement_mapper import stores                       # noqa: E402
from bank_statement_mapper.mapping_cache import MappingCache  # noqa: E402
from bank_statement_mapper.bank_mapper import ColumnMap   # noqa: E402


def test_factory_routes_urls_to_backends(tmp_path):
    assert isinstance(stores.open_store(None), stores.MemoryStore)
    assert isinstance(stores.open_store("memory://"), stores.MemoryStore)
    assert isinstance(stores.open_store(str(tmp_path / "c.json")), stores.JsonFileStore)
    assert isinstance(stores.open_store(str(tmp_path / "c.db")), stores.SqliteStore)
    assert isinstance(stores.open_store(f"sqlite:///{tmp_path/'x.db'}"), stores.SqliteStore)


@pytest.mark.parametrize("make", [
    lambda tp: stores.MemoryStore(),
    lambda tp: stores.JsonFileStore(str(tp / "s.json")),
    lambda tp: stores.SqliteStore(str(tp / "s.db")),
])
def test_store_roundtrip_and_upsert(make, tmp_path):
    s = make(tmp_path)
    assert s.get("missing") is None
    s.put("k", {"a": 1})
    assert s.get("k") == {"a": 1}
    s.put("k", {"a": 2})              # upsert
    assert s.get("k") == {"a": 2}
    s.close()


def test_sqlite_persists_across_reopen(tmp_path):
    path = str(tmp_path / "p.db")
    s1 = stores.SqliteStore(path)
    s1.put("k", {"v": 9})
    s1.close()
    s2 = stores.SqliteStore(path)
    assert s2.get("k") == {"v": 9}
    s2.close()


def test_mapping_cache_sqlite_backend(tmp_path):
    cache = MappingCache(f"sqlite:///{tmp_path/'m.db'}")
    header = ["Txn Date", "Narration", "Debit", "Credit"]
    maps = [
        ColumnMap(0, "Txn Date", "date", 100, "exact"),
        ColumnMap(1, "Narration", "description", 100, "exact"),
        ColumnMap(2, "Debit", "debit", 100, "exact"),
        ColumnMap(3, "Credit", "credit", 100, "exact"),
    ]
    assert cache.get(header) is None
    cache.put(header, maps)
    got = cache.get(header)
    assert [m.field for m in got] == ["date", "description", "debit", "credit"]
    assert all(m.method == "cache" for m in got)     # replayed entries marked cache
    # a different header layout is a cache miss
    assert cache.get(["Date", "Details", "Amount"]) is None
    cache.close()


def test_mapping_cache_legacy_path_kwarg(tmp_path):
    cache = MappingCache(path=str(tmp_path / "legacy.json"))
    assert isinstance(cache._store, stores.JsonFileStore)


def test_redis_valkey_postgres_urls_route(monkeypatch):
    # don't connect; just prove the factory dispatches (import will fail w/o dep)
    with pytest.raises(Exception):
        stores.open_store("redis://localhost:6379/0")     # redis not installed
    with pytest.raises(Exception):
        stores.open_store("valkey://localhost:6379/0")    # valkey/redis not installed
    with pytest.raises(Exception):
        stores.open_store("postgresql://u@localhost/db")  # psycopg not installed


class _FakeRedis:
    """Minimal redis/valkey-protocol client for testing the shared store logic."""
    def __init__(self): self.kv = {}
    def get(self, k): return self.kv.get(k)
    def set(self, k, v): self.kv[k] = v


def test_redis_protocol_store_shared_by_redis_and_valkey():
    s = stores._RedisProtocolStore(_FakeRedis(), prefix="bankmap:")
    assert s.get("k") is None
    s.put("k", {"a": 1}); assert s.get("k") == {"a": 1}
    s.put("k", {"a": 2}); assert s.get("k") == {"a": 2}   # overwrite
    assert issubclass(stores.RedisStore, stores._RedisProtocolStore)
    assert issubclass(stores.ValkeyStore, stores._RedisProtocolStore)


def test_valkey_client_scheme_normalization(monkeypatch):
    """_redis_proto_client picks a driver and normalizes the scheme for it."""
    captured = {}

    class _FakeMod:
        @staticmethod
        def from_url(u):
            captured["url"] = u
            return _FakeRedis()

    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "valkey":
            return _FakeMod
        if name == "redis":
            raise ImportError("no redis")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    stores._redis_proto_client("rediss://h:6379", prefer="valkey")
    assert captured["url"].startswith("valkeys://")   # normalized for valkey-py
