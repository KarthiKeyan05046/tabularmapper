"""
learn.py — self-learning synonym vocabulary.

When the AI (or a human) confirms that a header maps to a field, that phrase is
recorded here. Next time any bank uses that header it's a deterministic EXACT
match — the AI never fires for it again, and nobody edits code. Over time the
AI-call rate trends to zero.

Two halves, cleanly split:
  * CONFIG (schema.py / config.json) = seed vocabulary, read-only, from S3/URL.
  * LEARNED (this store)             = mutable, grows from real traffic.
Effective synonyms at match time = seed + learned (seed wins on conflict).

Storage uses the same URL convention as the cache (stores.open_store):
    LearnStore()                                  # env BANK_MAPPER_LEARN_STORE, else sqlite
    LearnStore("redis://localhost:6379/0")
    LearnStore("memory://")                        # tests

Trust policy (financial-data safe by default):
  * date / description / reference / balance / amount  -> auto-applied.
  * debit / credit                                     -> held in `pending` for a
    human to approve(), because a wrong debit/credit direction is the one costly
    error. Set auto_apply_gated=True to skip review (fully unattended).
"""

from __future__ import annotations

import os
import re
import time
from typing import Optional

from stores import open_store

_DEFAULT_URL = "learned_synonyms.db"
_KEY = "learned"
_DEFAULT_GATED = frozenset({"debit", "credit"})


def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s).strip().lower()) if s is not None else ""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class LearnStore:
    def __init__(self, source: Optional[str] = None, *,
                 gated_fields=_DEFAULT_GATED, auto_apply_gated: bool = False):
        url = source or os.getenv("BANK_MAPPER_LEARN_STORE") or _DEFAULT_URL
        self.url = url
        self._store = open_store(url)
        self.gated_fields = set(gated_fields)
        self.auto_apply_gated = auto_apply_gated

    # -- persistence (single provenance-rich record) --
    def _load(self) -> dict:
        return self._store.get(_KEY) or {
            "version": 1, "fields": {}, "pending": [], "conflicts": []}

    def _save(self, blob: dict) -> None:
        self._store.put(_KEY, blob)

    @staticmethod
    def _field_of(blob: dict, phrase: str) -> Optional[str]:
        for fld, entries in blob["fields"].items():
            if any(e["phrase"] == phrase for e in entries):
                return fld
        return None

    # -- read views --
    def synonyms(self) -> dict:
        """Applied learned synonyms as {field: [phrases]} (for matching)."""
        blob = self._load()
        return {f: [e["phrase"] for e in es] for f, es in blob["fields"].items() if es}

    def pending(self) -> list:
        return self._load()["pending"]

    def conflicts(self) -> list:
        return self._load()["conflicts"]

    def stats(self) -> dict:
        b = self._load()
        return {"applied": sum(len(v) for v in b["fields"].values()),
                "pending": len(b["pending"]), "conflicts": len(b["conflicts"])}

    # -- write path --
    def add(self, header: str, field: str, *, source: str = "ai",
            bank: Optional[str] = None) -> str:
        """Record a header->field mapping. Returns one of:
        'learned' | 'pending' | 'exists' | 'conflict' | 'skip'."""
        phrase = _norm(header)
        if not phrase or not field:
            return "skip"
        blob = self._load()
        existing = self._field_of(blob, phrase)
        if existing == field:
            return "exists"
        if existing is not None:                 # phrase already means something else
            blob["conflicts"].append({
                "phrase": phrase, "existing": existing, "proposed": field,
                "source": source, "ts": _now()})
            self._save(blob)
            return "conflict"
        entry = {"phrase": phrase, "field": field, "source": source,
                 "bank": bank, "ts": _now()}
        gated = (field in self.gated_fields and source in ("ai", "harvest")
                 and not self.auto_apply_gated)
        if gated:
            if not any(p["phrase"] == phrase for p in blob["pending"]):
                blob["pending"].append(entry)
            self._save(blob)
            return "pending"
        blob["fields"].setdefault(field, []).append(entry)
        self._save(blob)
        return "learned"

    # -- human review of gated entries --
    def approve(self, phrase: str, field: Optional[str] = None) -> bool:
        phrase = _norm(phrase)
        blob = self._load()
        keep, moved = [], False
        for p in blob["pending"]:
            if p["phrase"] == phrase and (field is None or p["field"] == field):
                blob["fields"].setdefault(p["field"], []).append(
                    dict(p, source="human", ts=_now()))
                moved = True
            else:
                keep.append(p)
        blob["pending"] = keep
        self._save(blob)
        return moved

    def reject(self, phrase: str, field: Optional[str] = None) -> bool:
        phrase = _norm(phrase)
        blob = self._load()
        before = len(blob["pending"])
        blob["pending"] = [p for p in blob["pending"]
                           if not (p["phrase"] == phrase and
                                   (field is None or p["field"] == field))]
        self._save(blob)
        return len(blob["pending"]) < before

    def close(self) -> None:
        self._store.close()


def learn_from_result(result, store: LearnStore, *, min_confidence: int = 85,
                      methods=("ai",), source: str = "ai",
                      bank: Optional[str] = None) -> dict:
    """Walk a ProcessResult's column maps and teach the store every confident,
    model-resolved (non-exact) mapping. Returns a summary keyed by outcome."""
    summary: dict[str, list] = {
        "learned": [], "pending": [], "exists": [], "conflict": [], "skip": []}
    for m in result.column_maps:
        if not m.field or m.method == "exact" or m.method not in methods:
            continue
        if m.confidence < min_confidence:
            continue
        outcome = store.add(m.raw_header, m.field, source=source, bank=bank)
        summary[outcome].append((m.raw_header, m.field))
    return summary


def harvest_folder(folder: str, store: LearnStore, *,
                   table_matcher=None, min_confidence: int = 85,
                   methods=("ai", "fuzzy"), recursive: bool = False) -> dict:
    """Bootstrap the vocabulary from a folder of past statements.

    Runs the mapper over every .xlsx in `folder`, and teaches the store each
    confident header->field pair that the seed synonyms didn't already resolve
    exactly (fuzzy + AI matches). Gated fields (debit/credit) land in the pending
    queue for a quick one-time review. Returns a report.

    Pass a `table_matcher` (OpenAICompatibleMatcher) to also resolve headers that
    fuzzy can't place; omit it to harvest deterministically only.
    """
    import glob
    from bank_mapper import process_file

    pattern = os.path.join(folder, "**", "*.xlsx") if recursive \
        else os.path.join(folder, "*.xlsx")
    report: dict = {"files": 0, "learned": [], "pending": [],
                    "exists": [], "conflict": [], "skip": [], "errors": []}
    for path in sorted(glob.glob(pattern, recursive=recursive)):
        bank = os.path.splitext(os.path.basename(path))[0]
        try:
            res = process_file(path, table_matcher=table_matcher)
        except Exception as exc:  # noqa: BLE001 — a bad file shouldn't abort the batch
            report["errors"].append((os.path.basename(path), str(exc)))
            continue
        report["files"] += 1
        summ = learn_from_result(res, store, min_confidence=min_confidence,
                                 methods=methods, source="harvest", bank=bank)
        for outcome, pairs in summ.items():
            report.setdefault(outcome, []).extend(
                (os.path.basename(path), h, f) for h, f in pairs)
    report["stats"] = store.stats()
    return report
