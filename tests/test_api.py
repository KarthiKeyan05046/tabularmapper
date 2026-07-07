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
    monkeypatch.setenv("TABULARMAPPER_CACHE", str(tmp_path / "cache.json"))
    monkeypatch.setenv("TABULARMAPPER_LEARN_STORE", "memory://")
    monkeypatch.setenv("TABULARMAPPER_CONFIG", os.path.join(ROOT, "config.example.json"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    import importlib
    import tabularmapper.api as api
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


def test_map_format_base64(client):
    import base64
    with open(os.path.join(FIX, "01_junk_split.xlsx"), "rb") as fh:
        payload = fh.read()
    r = client.post("/mapper/map", params={"format": "base64"},
                    files={"file": ("stmt.xlsx", io.BytesIO(payload))})
    assert r.status_code == 200
    body = r.json()
    # rows still inline...
    assert any(t["credit"] == 45000.0 for t in body["transactions"])
    # ...plus a base64 .xlsx that decodes to a real zip (xlsx magic = PK)
    assert body["file_base64"]
    assert base64.b64decode(body["file_base64"])[:2] == b"PK"


def test_map_format_file_download(client):
    with open(os.path.join(FIX, "01_junk_split.xlsx"), "rb") as fh:
        payload = fh.read()
    r = client.post("/mapper/map", params={"format": "file"},
                    files={"file": ("statement.xlsx", io.BytesIO(payload))})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert 'attachment; filename="statement_mapped.xlsx"' in r.headers["content-disposition"]
    assert r.content[:2] == b"PK"          # real .xlsx bytes


def test_map_default_format_has_no_file_base64(client):
    with open(os.path.join(FIX, "01_junk_split.xlsx"), "rb") as fh:
        payload = fh.read()
    r = client.post("/mapper/map", files={"file": ("stmt.xlsx", io.BytesIO(payload))})
    assert r.json()["file_base64"] is None


def test_map_rejects_bad_format(client):
    with open(os.path.join(FIX, "01_junk_split.xlsx"), "rb") as fh:
        payload = fh.read()
    r = client.post("/mapper/map", params={"format": "pdf"},
                    files={"file": ("stmt.xlsx", io.BytesIO(payload))})
    assert r.status_code == 422       # fails the regex pattern


def test_map_rejects_non_xlsx(client):
    r = client.post("/mapper/map",
                    files={"file": ("notes.txt", io.BytesIO(b"hello"), "text/plain")})
    assert r.status_code == 400


def _fuzzy_xlsx_bytes():
    """A tiny bank sheet whose 'Descriptn' header only fuzzy-matches (score 90)."""
    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active
    ws.append(["Date", "Descriptn", "Debit", "Credit"])
    ws.append(["01-06-2026", "Coffee", "150", ""])
    ws.append(["02-06-2026", "Salary", "", "45000"])
    buf = io.BytesIO(); wb.save(buf); return buf.getvalue()


def test_map_threshold_query_changes_mapping(client):
    payload = _fuzzy_xlsx_bytes()

    # default gate (80): 'Descriptn' (score 90) is accepted as fuzzy
    r = client.post("/mapper/map", files={"file": ("s.xlsx", io.BytesIO(payload))})
    cols = {c["raw_header"]: c for c in r.json()["columns"]}
    assert cols["Descriptn"]["field"] == "description"
    assert cols["Descriptn"]["method"] == "fuzzy"

    # raise the gate above 90: the same column now falls through -> unmapped
    r = client.post("/mapper/map", params={"threshold": 95},
                    files={"file": ("s.xlsx", io.BytesIO(payload))})
    cols = {c["raw_header"]: c for c in r.json()["columns"]}
    assert cols["Descriptn"]["field"] is None


def test_map_threshold_out_of_range(client):
    payload = _fuzzy_xlsx_bytes()
    for bad in (150, -1):
        r = client.post("/mapper/map", params={"threshold": bad},
                        files={"file": ("s.xlsx", io.BytesIO(payload))})
        assert r.status_code == 422       # ge=0 / le=100 validation


def test_default_threshold_reads_env(monkeypatch):
    import tabularmapper.api as api
    monkeypatch.setenv("TABULARMAPPER_THRESHOLD", "90")
    assert api._default_threshold() == 90
    monkeypatch.setenv("TABULARMAPPER_THRESHOLD", "banana")   # invalid -> falls back
    assert api._default_threshold() == 80
    monkeypatch.delenv("TABULARMAPPER_THRESHOLD", raising=False)
    assert api._default_threshold() == 80


def test_config_page_served(client):
    r = client.get("/mapper/config")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "<!DOCTYPE html>" in r.text or "<html" in r.text.lower()


def test_active_config_json(client):
    # the client fixture loads config.example.json (the bank layout)
    r = client.get("/mapper/config.json")
    assert r.status_code == 200
    body = r.json()
    fields = [f["field"] for f in body["output_schema"]]
    assert "date" in fields and "debit" in fields
    assert body["synonyms"]                      # non-empty
    assert body["critical_fields"] == ["date"]


def test_router_prefix_default_and_custom():
    import tabularmapper.api as api
    assert {r.path for r in api.router.routes} == {
        "/mapper/health", "/mapper/config", "/mapper/config.json", "/mapper/test",
        "/mapper/map",
        "/mapper/learn/pending", "/mapper/learn/approve", "/mapper/learn/reject"}
    custom = api.make_router("/catalog/")
    assert "/catalog/map" in {r.path for r in custom.routes}
    assert "/statements/map" not in {r.path for r in custom.routes}


def test_router_prefix_from_env(monkeypatch):
    monkeypatch.setenv("TABULARMAPPER_ROUTE_PREFIX", "/ingest")
    import tabularmapper.api as api
    assert "/ingest/map" in {r.path for r in api.make_router().routes}
