"""
pytest suite for tabularmapper (bank preset fixtures).

Asserts per fixture: correct header index, correct field-per-column, correct
debit/credit split, correct normalized dates, and the right needs_review value.
Also covers the normalizers directly and the offline fallback path.

Run:  pytest -q   (from repo root)
"""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from tabularmapper.engine import (  # noqa: E402
    detect_header_row, map_columns, normalize_amount, normalize_date,
    process_file,
)
from tabularmapper import engine as _engine, bank_preset  # noqa: E402

FIX = os.path.join(ROOT, "test_statements")


@pytest.fixture(autouse=True)
def _bank_preset():
    """These fixtures are bank statements — activate the bank preset."""
    _engine.configure(config=bank_preset())
    yield
    _engine.configure()


def _fields(res):
    return {m.field: m.col_index for m in res.column_maps if m.field}


# --------------------------------------------------------------------------
# Normalizers
# --------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("1,200.50", 1200.50),
    ("(500)", -500.0),
    ("500 Dr", -500.0),
    ("₹500 Cr", 500.0),
    ("-1299", -1299.0),
    ("3500", 3500.0),
    ("  12,000.75 ", 12000.75),
    ("", None),
    (None, None),
])
def test_normalize_amount(raw, expected):
    assert normalize_amount(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("2025/06/01", "2025-06-01"),   # year-first, NOT flipped
    ("2025-06-01", "2025-06-01"),
    ("01-06-2025", "2025-06-01"),   # dd-mm-yyyy default
    ("12 Jun 2026", "2026-06-12"),
    ("", None),
])
def test_normalize_date(raw, expected):
    assert normalize_date(raw) == expected


def test_year_first_not_flipped():
    # 2025/06/01 must be June 1st, never Jan 6th
    assert normalize_date("2025/06/01") == "2025-06-01"


# --------------------------------------------------------------------------
# Fixture 1 — junk rows on top + split Debit/Credit
# --------------------------------------------------------------------------
def test_junk_split():
    res = process_file(os.path.join(FIX, "01_junk_split.xlsx"))
    assert res.header_index == 5
    f = _fields(res)
    assert f["date"] == 0 and f["description"] == 1 and f["reference"] == 2
    assert f["debit"] == 3 and f["credit"] == 4
    # UPI payment -> debit 1299.50, salary -> credit 45000
    recs = res.records
    assert any(r["debit"] == 1299.50 for r in recs)
    assert any(r["credit"] == 45000.00 for r in recs)
    assert recs[0]["date"] == "2025-04-01"
    assert res.needs_review is False


# --------------------------------------------------------------------------
# Fixture 2 — title row + single signed Amount
# --------------------------------------------------------------------------
def test_title_signed():
    res = process_file(os.path.join(FIX, "02_title_signed.xlsx"))
    assert res.header_index == 1
    f = _fields(res)
    assert "amount" in f and "debit" not in f and "credit" not in f
    recs = res.records
    # yyyy/mm/dd preserved
    assert recs[0]["date"] == "2025-06-01"
    # +3500 -> credit ; -1299 -> debit ; 500 Dr -> debit
    assert recs[0]["credit"] == 3500.0 and recs[0]["debit"] is None
    assert recs[1]["debit"] == 1299.0 and recs[1]["credit"] is None
    assert recs[3]["debit"] == 500.0


# --------------------------------------------------------------------------
# Fixture 3 — header row 1 + bracket negatives
# --------------------------------------------------------------------------
def test_header_row1_brackets():
    res = process_file(os.path.join(FIX, "03_header_row1_brackets.xlsx"))
    assert res.header_index == 0
    f = _fields(res)
    assert "amount" in f and "balance" in f
    recs = res.records
    # (299) -> debit 299 ; 150 -> credit 150 ; (8500) -> debit 8500
    assert any(r["debit"] == 299.0 for r in recs)
    assert any(r["credit"] == 150.0 for r in recs)
    assert any(r["debit"] == 8500.0 for r in recs)
    assert recs[0]["date"] == "2025-06-01"


# --------------------------------------------------------------------------
# Fixture 4 — garbage -> needs_review, no crash
# --------------------------------------------------------------------------
def test_garbage_needs_review():
    res = process_file(os.path.join(FIX, "04_garbage.xlsx"))
    assert res.needs_review is True
    assert res.review_reasons  # non-empty


# --------------------------------------------------------------------------
# Fixture 5 — weird header resolves only via offline fallback
# --------------------------------------------------------------------------
def test_weird_header_needs_review_without_fallback():
    res = process_file(os.path.join(FIX, "05_weird_header.xlsx"))
    # without a fallback, unknown money columns -> review
    assert res.needs_review is True


def test_weird_header_with_hashing_fallback():
    from tabularmapper.llm_fallback import HashingEmbeddingFallback
    res = process_file(os.path.join(FIX, "05_weird_header.xlsx"),
                       llm_fallback=HashingEmbeddingFallback())
    f = _fields(res)
    # date/desc/ref already resolve deterministically; the offline fallback
    # must recover the money columns that otherwise trip needs_review.
    assert "date" in f and "description" in f
    assert {"debit", "credit"}.issubset(set(f)) or "amount" in f
    recs = res.records
    assert len(recs) == 3
    assert any(r["debit"] == 150.0 for r in recs)
    assert any(r["credit"] == 50000.0 for r in recs)


# --------------------------------------------------------------------------
# Zero network calls with fallback=None
# --------------------------------------------------------------------------
def test_no_network_without_fallback(monkeypatch):
    import socket

    def _boom(*a, **k):
        raise AssertionError("network call attempted with fallback=None")

    monkeypatch.setattr(socket, "socket", _boom)
    for name in ["01_junk_split.xlsx", "02_title_signed.xlsx",
                 "03_header_row1_brackets.xlsx", "04_garbage.xlsx"]:
        process_file(os.path.join(FIX, name))  # must not touch the network


# --------------------------------------------------------------------------
# In-memory processing (direct binary — no temp file / disk)
# --------------------------------------------------------------------------
def test_process_stream_from_bytes_matches_path():
    import io
    from tabularmapper.engine import process_stream
    p = os.path.join(FIX, "01_junk_split.xlsx")
    with open(p, "rb") as fh:
        raw = fh.read()

    ref = process_file(p)
    a = process_stream(raw)                 # bytes
    b = process_stream(io.BytesIO(raw))     # file-like

    for res in (a, b):
        assert res.header_index == ref.header_index
        assert _fields(res) == _fields(ref)
        assert res.records == ref.records
        assert res.needs_review is False


# --------------------------------------------------------------------------
# AI table matcher (mocked transport — NO network in tests)
# --------------------------------------------------------------------------
import json as _json  # noqa: E402
from tabularmapper.ai_matcher import OpenAICompatibleMatcher, profile_columns  # noqa: E402


def test_profiler_detects_mutual_exclusivity():
    """Debit/Credit columns are never filled in the same row -> the profiler
    must surface that structural signal (this is how the AI infers a d/c pair)."""
    header = ["Txn Date", "Narration", "Ref No.", "Outgoing", "Incoming"]
    rows = [
        ["01-06-2025", "Coffee", "D1", "150", None],
        ["02-06-2025", "Salary", "D2", None, "50000"],
        ["03-06-2025", "Rent", "D3", "2300", None],
    ]
    profs = profile_columns(header, rows)
    out, inc = profs[3], profs[4]
    assert 4 in out["mutually_exclusive_with"]
    assert 3 in inc["mutually_exclusive_with"]
    assert out["dtype"] == "number" and inc["dtype"] == "number"


def _fake_ai(mapping: dict):
    """Return a transport that echoes a fixed mapping AND records what was sent,
    so we can assert no real cell data leaves the machine."""
    sent = {}

    def transport(messages):
        sent["messages"] = messages
        return _json.dumps({str(k): v for k, v in mapping.items()})
    return transport, sent


def test_ai_matcher_maps_unknown_headers_and_caches(tmp_path):
    from tabularmapper.mapping_cache import MappingCache
    transport, sent = _fake_ai({0: "date", 1: "description", 2: "reference",
                                3: "debit", 4: "credit"})
    matcher = OpenAICompatibleMatcher(api_key="x", transport=transport)
    cache = MappingCache(path=str(tmp_path / "c.json"))

    res = process_file(os.path.join(FIX, "05_weird_header.xlsx"),
                       table_matcher=matcher, cache=cache)
    f = _fields(res)
    assert f["debit"] == 3 and f["credit"] == 4      # AI filled the money cols
    assert res.needs_review is False                 # complete -> trusted
    assert len(res.records) == 3
    assert any(r["debit"] == 150.0 for r in res.records)
    assert any(r["credit"] == 50000.0 for r in res.records)

    # cached -> a second run must NOT call the AI again
    def _boom(_m):
        raise AssertionError("AI called on a cached format")
    matcher2 = OpenAICompatibleMatcher(api_key="x", transport=_boom)
    res2 = process_file(os.path.join(FIX, "05_weird_header.xlsx"),
                        table_matcher=matcher2, cache=cache)
    assert _fields(res2)["debit"] == 3


def test_ai_matcher_sends_no_real_data():
    """Privacy contract: the prompt carries headers + structural metadata only —
    never a transaction value, name, amount, or narration."""
    transport, sent = _fake_ai({0: "date", 3: "debit", 4: "credit"})
    matcher = OpenAICompatibleMatcher(api_key="x", transport=transport)
    process_file(os.path.join(FIX, "05_weird_header.xlsx"), table_matcher=matcher)
    blob = _json.dumps(sent["messages"])
    for leak in ["Coffee", "Salary", "Rent", "150", "50000", "2300", "D1", "D2"]:
        assert leak not in blob, f"real data leaked into prompt: {leak}"
    # but the structural facts ARE present
    assert "Outgoing" in blob and "mutually-exclusive" in blob


def test_ai_system_prompt_is_configurable():
    """Default is domain-neutral; an explicit prompt overrides it, and the
    JSON-format contract in the user message survives either way."""
    from tabularmapper.ai_matcher import DEFAULT_SYSTEM_PROMPT
    profs = [{"index": 0, "name": "Date", "dtype": "date", "fill_rate": 1.0,
              "has_negative": False, "mutually_exclusive_with": []}]

    d = OpenAICompatibleMatcher(api_key="x")
    sys_default = d._build_messages(profs, ["date"])[0]["content"]
    assert "bank-statement" not in sys_default
    assert sys_default == DEFAULT_SYSTEM_PROMPT

    c = OpenAICompatibleMatcher(api_key="x", system_prompt="MAP PRODUCTS ONLY.")
    msgs = c._build_messages(profs, ["date"])
    assert msgs[0]["content"] == "MAP PRODUCTS ONLY."
    assert "Return JSON" in msgs[1]["content"] and "exactly once" in msgs[1]["content"]


def test_ai_system_prompt_from_config():
    from tabularmapper import config_from_dict
    from tabularmapper.schema import config_to_dict
    cfg = config_from_dict({"output_schema": [{"field": "date", "type": "date"}],
                            "synonyms": {}, "ai_system_prompt": "CUSTOM"})
    assert cfg.ai_system_prompt == "CUSTOM"
    assert config_to_dict(cfg)["ai_system_prompt"] == "CUSTOM"


def test_ai_fill_all_is_default(monkeypatch, tmp_path):
    """Default TABULARMAPPER_AI_FILL='all': AI fires on ANY unmapped column and
    fills non-critical leftovers. 'critical' restricts it to critical gaps."""
    import json as _J
    from openpyxl import Workbook
    from tabularmapper import configure, config_from_dict

    configure(config_from_dict({
        "output_schema": [{"field": "date", "type": "date"},
                          {"field": "reference", "type": "string"},
                          {"field": "debit", "type": "currency"},
                          {"field": "credit", "type": "currency"}],
        "synonyms": {"date": ["date"], "reference": ["reference"],
                     "debit": ["debit"], "credit": ["credit"]},
        "critical_fields": ["date", "debit", "credit"]}))

    wb = Workbook(); ws = wb.active
    ws.append(["Date", "Ref/Chq No", "Debit", "Credit"])   # Ref/Chq No won't match 'reference'
    ws.append(["01-06-2025", "X1", "100", ""])
    p = str(tmp_path / "s.xlsx"); wb.save(p)

    calls = {"n": 0}
    def transport(_m):
        calls["n"] += 1
        return _J.dumps({"1": "reference"})
    mk = lambda: OpenAICompatibleMatcher(api_key="x", transport=transport)

    # default (env absent) -> AI fires and fills the non-critical reference column
    monkeypatch.delenv("TABULARMAPPER_AI_FILL", raising=False)
    r = process_file(p, table_matcher=mk())
    f = {m.raw_header: m for m in r.column_maps}
    assert f["Ref/Chq No"].field == "reference" and f["Ref/Chq No"].method == "ai"
    assert calls["n"] == 1

    # critical -> date/debit/credit already mapped => no gap => AI never called
    calls["n"] = 0
    monkeypatch.setenv("TABULARMAPPER_AI_FILL", "critical")
    r = process_file(p, table_matcher=mk())
    f = {m.raw_header: m for m in r.column_maps}
    assert f["Ref/Chq No"].field is None
    assert calls["n"] == 0


def test_ai_matcher_graceful_on_transport_error():
    """A network/API failure must not crash — columns just stay unmapped."""
    def boom(_m):
        raise RuntimeError("api down")
    matcher = OpenAICompatibleMatcher(api_key="x", transport=boom)
    res = process_file(os.path.join(FIX, "05_weird_header.xlsx"), table_matcher=matcher)
    assert res.needs_review is True   # unresolved, but no crash


# --------------------------------------------------------------------------
# C13 regression — split two-row header must not corrupt amounts/direction
# --------------------------------------------------------------------------
def test_c13_split_multirow_header_no_corruption():
    """Regression for the C13 stress bug: a header split across two physical rows
    ('Withdrawal | Deposit' over 'Amount | Amount') must NOT collapse both money
    columns into one — which previously booked a withdrawal as a credit and
    dropped a 45,000 credit entirely, silently."""
    p = os.path.join(ROOT, "test_statements", "confusing",
                     "C13_split_multirow_header.xlsx")
    if not os.path.exists(p):
        pytest.skip("C13 fixture not present")
    res = process_file(p)
    f = {m.raw_header: m for m in res.column_maps}
    # the wrapped header is recovered -> two DISTINCT money columns
    assert f["Withdrawal Amount"].field == "debit"
    assert f["Deposit Amount"].field == "credit"
    assert res.needs_review is False
    recs = res.records
    # withdrawal 1299.50 is a DEBIT (was a phantom credit); the 45,000 deposit
    # survives as a CREDIT (was dropped)
    assert any(r["debit"] == 1299.50 and r.get("credit") is None for r in recs)
    assert any(r["credit"] == 45000.0 and r.get("debit") is None for r in recs)
    # and the withdrawal is never mis-booked as a credit anywhere
    assert not any(r.get("credit") == 1299.50 for r in recs)


def test_mutex_numeric_dup_flags_review_backstop(tmp_path):
    """When a split header can't be recovered (single-row header, two identical
    'Amount' columns), two mutually-exclusive numeric columns collapsing to one
    field must flag needs_review — never silently drop one as a plain `dup`."""
    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active
    ws.append(["Date", "Narration", "Amount", "Amount"])   # no fragment row above
    ws.append(["01-06-2025", "UPI PAY", "1299.50", ""])
    ws.append(["02-06-2025", "SALARY", "", "45000"])
    p = str(tmp_path / "two_amount.xlsx"); wb.save(p)
    res = process_file(p)
    assert res.needs_review is True
    assert any("collapsed to one field" in r for r in res.review_reasons)
    assert any(m.method == "dup_pair" for m in res.column_maps)


# --------------------------------------------------------------------------
# Real-world files (present in repo root)
# --------------------------------------------------------------------------
def test_real_payir_header_deep():
    p = os.path.join(ROOT, "samples", "PAYIR_FC_SBI_2025.xlsx")
    if not os.path.exists(p):
        pytest.skip("real file not present")
    res = process_file(p)
    assert res.header_index == 19       # metadata stacked above
    f = _fields(res)
    assert f["date"] is not None and "debit" in f and "credit" in f
    assert res.needs_review is False
    assert len(res.records) > 10
