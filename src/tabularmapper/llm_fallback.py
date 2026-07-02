"""
llm_fallback.py — pluggable fallback adapters for map_columns().

Interface (matches engine.map_columns' `llm_fallback` param):

    fallback(header: str, samples: list[str], allowed_fields: list[str]) -> str | None

Contract: the callable receives ONLY a header string, up to 3 sample cell
strings, and the list of allowed field keys. It never sees full transaction
rows. It returns one of `allowed_fields` or None. This keeps bank data local
and auditable.

Adapters here are the PER-COLUMN, offline degraded path:
  * HashingEmbeddingFallback — zero-dependency char-ngram cosine. Lexical only
                            (weak), but runs fully air-gapped with no model file
                            and no API. A last resort when the AI matcher is off
                            or unreachable.
  * make_llm_fallback     — wrap any 'text -> text' model into the per-column
                            interface (OFF by default).

For the primary, high-accuracy path — a real LLM that reads the whole header row
plus structural profiles and returns a full mapping — see `ai_matcher.py`
(OpenAICompatibleMatcher). That is the recommended way to auto-map new banks.

None of these fire unless the deterministic exact+fuzzy matcher fails a header.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Callable, Optional

# Human-readable descriptions per field. The embedding models compare the raw
# header against these, so richer phrasing -> better cosine separation.
FIELD_DESCRIPTIONS: dict[str, str] = {
    "date": "date of the transaction when it was posted or valued",
    "description": "narration particulars description details remarks of the transaction",
    "reference": "reference number cheque number transaction id utr instrument number code",
    "debit": "debit withdrawal money going out paid out outgoing spent amount reducing balance",
    "credit": "credit deposit money coming in paid in incoming received income amount increasing balance",
    "balance": "account balance remaining after the transaction closing running available balance",
    "amount": "single signed transaction amount positive or negative value",
}


# --------------------------------------------------------------------------
# 2) Zero-dependency offline fallback (no model download)
# --------------------------------------------------------------------------
class HashingEmbeddingFallback:
    """Char-ngram cosine similarity. No torch, no download, fully offline.

    Lexical only, so weaker than the AI matcher — but it needs no API, no model
    file, and runs fully air-gapped / in CI. Same interface & contract.
    """

    def __init__(self, min_similarity: float = 0.18, ngram: int = 3,
                 field_descriptions: Optional[dict] = None):
        self.min_similarity = min_similarity
        self.ngram = ngram
        self.field_descriptions = field_descriptions or FIELD_DESCRIPTIONS
        self._field_vecs = {
            f: self._vec(d) for f, d in self.field_descriptions.items()
        }

    def _vec(self, text: str) -> Counter:
        t = re.sub(r"[^a-z0-9]", " ", text.lower())
        grams: Counter = Counter()
        for tok in t.split():
            grams[tok] += 1  # word tokens
            padded = f" {tok} "
            for i in range(len(padded) - self.ngram + 1):
                grams[padded[i:i + self.ngram]] += 1
        return grams

    @staticmethod
    def _cos(a: Counter, b: Counter) -> float:
        common = set(a) & set(b)
        num = sum(a[k] * b[k] for k in common)
        na = math.sqrt(sum(v * v for v in a.values()))
        nb = math.sqrt(sum(v * v for v in b.values()))
        return num / (na * nb) if na and nb else 0.0

    def __call__(self, header: str, samples: list[str],
                 allowed_fields: list[str]) -> Optional[str]:
        qv = self._vec(header + " " + " ".join(samples))
        best_field, best_sim = None, -1.0
        for fld in allowed_fields:
            fv = self._field_vecs.get(fld) or self._vec(fld)
            sim = self._cos(qv, fv)
            if sim > best_sim:
                best_field, best_sim = fld, sim
        return best_field if best_sim >= self.min_similarity else None


# --------------------------------------------------------------------------
# Optional hosted small-model adapter (OFF by default)
# --------------------------------------------------------------------------
def make_llm_fallback(client_call: Callable[[str], str]) -> Callable:
    """Wrap any 'text -> text' small-model call into the fallback interface.

    `client_call(prompt)` should return a single field name. This never sends
    transaction rows — only the header + samples + allowed fields.
    """
    def _fallback(header: str, samples: list[str],
                  allowed_fields: list[str]) -> Optional[str]:
        prompt = (
            "You map one spreadsheet column header to exactly one field.\n"
            f"Allowed fields: {', '.join(allowed_fields)}\n"
            f"Header: {header!r}\n"
            f"Sample values: {samples}\n"
            "Reply with ONLY the single best field name, or 'none'."
        )
        ans = (client_call(prompt) or "").strip().lower()
        ans = re.sub(r"[^a-z]", "", ans)
        return ans if ans in allowed_fields else None
    return _fallback
