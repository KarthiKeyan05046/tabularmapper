"""
FastAPI router tests — skipped automatically if fastapi isn't installed
(install with `pip install -e ".[api]"`).
"""

import io
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("multipart")  # python-multipart, needed for UploadFile
from fastapi.testclient import TestClient  # noqa: E402

FIX = os.path.join(ROOT, "test_statements")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # isolate the cache file; keep AI off (no key); learn store in memory;
    # the fixtures are bank statements -> load the bank preset via config.
    monkeypatch.setenv("SCHEMA_MAPPER_CACHE", str(tmp_path / "cache.json"))
    monkeypatch.setenv("SCHEMA_MAPPER_LEARN_STORE", "memory://")
    monkeypatch.setenv("SCHEMA_MAPPER_CONFIG", os.path.join(ROOT, "config.example.json"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    import importlib
    import schema_mapper.api as api
    importlib.reload(api)
    with TestClient(api.app) as c:   # `with` runs lifespan
        yield c


def test_health(client):
    r = client.get("/mapper/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["ai_enabled"] is False       # no OPENAI_API_KEY


def test_map_deterministic(client):
    with open(os.path.join(FIX, "01_junk_split.xlsx"), "rb") as fh:
        payload = fh.read()
    r = client.post(
        "/mapper/map",
        files={"file": ("stmt.xlsx", io.BytesIO(payload),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["header_index"] == 5
    assert body["needs_review"] is False
    fields = {c["field"]: c["col_index"] for c in body["columns"] if c["field"]}
    assert fields["date"] == 0 and fields["debit"] == 3 and fields["credit"] == 4
    assert any(t["credit"] == 45000.0 for t in body["transactions"])
    assert body["schema_columns"][0] == "Date"


def test_map_rejects_non_xlsx(client):
    r = client.post("/mapper/map",
                    files={"file": ("notes.txt", io.BytesIO(b"hello"), "text/plain")})
    assert r.status_code == 400


def test_router_prefix_default_and_custom():
    import schema_mapper.api as api
    assert {r.path for r in api.router.routes} == {
        "/mapper/health", "/mapper/map",
        "/mapper/learn/pending", "/mapper/learn/approve", "/mapper/learn/reject"}
    custom = api.make_router("/catalog/")
    assert "/catalog/map" in {r.path for r in custom.routes}
    assert "/statements/map" not in {r.path for r in custom.routes}


def test_router_prefix_from_env(monkeypatch):
    monkeypatch.setenv("SCHEMA_MAPPER_ROUTE_PREFIX", "/ingest")
    import schema_mapper.api as api
    assert "/ingest/map" in {r.path for r in api.make_router().routes}
