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
    # isolate the cache file; keep AI off (no key); learn store in memory
    monkeypatch.setenv("BANK_MAPPER_CACHE", str(tmp_path / "cache.json"))
    monkeypatch.setenv("BANK_MAPPER_LEARN_STORE", "memory://")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    import importlib
    import bank_statement_mapper.bank_mapper_api as bank_mapper_api
    importlib.reload(bank_mapper_api)
    with TestClient(bank_mapper_api.app) as c:   # `with` runs lifespan
        yield c


def test_health(client):
    r = client.get("/statements/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["ai_enabled"] is False       # no OPENAI_API_KEY


def test_map_deterministic(client):
    with open(os.path.join(FIX, "01_junk_split.xlsx"), "rb") as fh:
        payload = fh.read()
    r = client.post(
        "/statements/map",
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
    r = client.post("/statements/map",
                    files={"file": ("notes.txt", io.BytesIO(b"hello"), "text/plain")})
    assert r.status_code == 400
