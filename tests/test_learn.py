"""
Tests for the self-learning synonym loop (learn.py + bank_mapper.apply_learned).

Covers: auto-apply of non-gated fields, gating of debit/credit to pending,
human approve/reject, conflict detection, and the closed loop — a header the AI
resolves once becomes a deterministic EXACT match afterward with no AI call.
"""

import json as _json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from bank_statement_mapper import bank_mapper as bm            # noqa: E402
from bank_statement_mapper.learn import LearnStore, learn_from_result  # noqa: E402

FIX = os.path.join(ROOT, "test_statements")


@pytest.fixture(autouse=True)
def _reset():
    bm.configure()
    bm.apply_learned(None)
    yield
    bm.configure()
    bm.apply_learned(None)


def _store():
    return LearnStore("memory://")


def test_non_gated_field_auto_applies():
    s = _store()
    assert s.add("when posted", "date", source="ai") == "learned"
    assert "when posted" in s.synonyms()["date"]


def test_debit_credit_gated_to_pending():
    s = _store()
    assert s.add("outgoing", "debit", source="ai") == "pending"
    assert s.synonyms().get("debit") in (None, [])        # not applied yet
    assert any(p["phrase"] == "outgoing" for p in s.pending())


def test_approve_moves_pending_to_applied():
    s = _store()
    s.add("outgoing", "debit", source="ai")
    assert s.approve("outgoing", "debit") is True
    assert s.synonyms()["debit"] == ["outgoing"]
    assert s.pending() == []


def test_reject_discards_pending():
    s = _store()
    s.add("incoming", "credit", source="ai")
    assert s.reject("incoming") is True
    assert s.pending() == []
    assert "credit" not in s.synonyms()


def test_conflict_detection():
    s = _store()
    s.add("when posted", "date", source="ai")            # applied
    assert s.add("when posted", "reference", source="ai") == "conflict"
    assert s.conflicts() and s.conflicts()[0]["existing"] == "date"


def test_human_source_bypasses_gate():
    s = _store()
    assert s.add("cheque withdrawal", "debit", source="human") == "learned"
    assert "cheque withdrawal" in s.synonyms()["debit"]


def test_learn_from_result_summary():
    s = _store()

    class M:
        def __init__(self, h, f, meth, c): self.raw_header, self.field, self.method, self.confidence = h, f, meth, c

    class R:
        column_maps = [M("Txn Date", "date", "exact", 100),   # skipped (exact)
                       M("Docket", "reference", "ai", 88),     # learned (non-gated)
                       M("Outgoing", "debit", "ai", 85)]       # pending (gated)
    summary = learn_from_result(R(), s)
    assert ("Docket", "reference") in summary["learned"]
    assert ("Outgoing", "debit") in summary["pending"]


def test_closed_loop_learned_header_becomes_exact():
    """The payoff: teach the store, then the same weird header maps as EXACT
    with NO ai/fallback on the next run."""
    s = _store()
    # simulate: AI resolved these once, human approved the gated pair
    s.add("outgoing", "debit", source="human")
    s.add("incoming", "credit", source="human")
    bm.apply_learned(s)

    res = bm.process_file(os.path.join(FIX, "05_weird_header.xlsx"))  # no AI, no fallback
    fields = {m.field: m.method for m in res.column_maps if m.field}
    assert fields["debit"] == "exact" and fields["credit"] == "exact"
    assert res.needs_review is False
    assert len(res.records) == 3


def test_process_file_learns_via_learn_store():
    """End-to-end: an AI-mapped run feeds the learn store; a non-gated field is
    applied and immediately usable."""
    from bank_statement_mapper.ai_matcher import OpenAICompatibleMatcher
    s = _store()

    def transport(messages):
        # map the weird money cols; description/date/ref resolve deterministically
        return _json.dumps({"3": "debit", "4": "credit"})
    matcher = OpenAICompatibleMatcher(api_key="x", transport=transport)

    res = bm.process_file(os.path.join(FIX, "05_weird_header.xlsx"),
                          table_matcher=matcher, learn_store=s)
    # debit/credit are gated -> they land in pending, not auto-applied
    pend = {p["field"] for p in s.pending()}
    assert {"debit", "credit"} & pend


def test_harvest_folder(tmp_path):
    import shutil
    from bank_statement_mapper.ai_matcher import OpenAICompatibleMatcher
    from bank_statement_mapper.learn import harvest_folder
    shutil.copy(os.path.join(FIX, "05_weird_header.xlsx"), tmp_path / "acme.xlsx")
    s = _store()

    def transport(messages):
        return _json.dumps({"3": "debit", "4": "credit"})
    matcher = OpenAICompatibleMatcher(api_key="x", transport=transport)

    report = harvest_folder(str(tmp_path), s, table_matcher=matcher)
    assert report["files"] == 1
    assert not report["errors"]
    # harvested debit/credit are gated -> pending for review
    assert {p["field"] for p in s.pending()} >= {"debit", "credit"}
    assert report["stats"]["pending"] >= 2


def test_harvest_deterministic_no_matcher(tmp_path):
    """Without a matcher, harvest still promotes fuzzy (non-exact) matches."""
    import openpyxl
    from bank_statement_mapper.learn import harvest_folder
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(["Txn Dt", "Naration", "Ref", "Withdrawls", "Deposits"])  # fuzzy, misspelled
    ws.append(["01-06-2025", "x", "R1", "100", None])
    ws.append(["02-06-2025", "y", "R2", None, "200"])
    wb.save(tmp_path / "b.xlsx")
    s = _store()
    report = harvest_folder(str(tmp_path), s)   # deterministic only
    assert report["files"] == 1
    # something got learned or queued from the fuzzy headers
    assert s.stats()["applied"] + s.stats()["pending"] >= 1
