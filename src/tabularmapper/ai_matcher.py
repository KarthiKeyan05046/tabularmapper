"""
ai_matcher.py — LLM-based, table-level column matcher for NEW bank layouts.

This is the high-accuracy path your boss is asking for: when a statement's
header is unknown to the synonym table, one LLM call maps the whole header row
to the output fields and the result is written straight into mapping_cache.json,
so that bank is "known" forever after (never hits the LLM again).

PRIVACY — the model matches the TABLE, never the data
-----------------------------------------------------
The prompt contains ONLY:
  * column header strings (e.g. "Withdrawals", "Value Dt")
  * a structural profile per column computed locally (dtype, sign, fill-rate,
    which columns are mutually exclusive) — this is metadata, NOT cell contents
  * the list of allowed output fields + short descriptions

It NEVER contains transaction amounts, dates, names, narrations or references.
No real statement data leaves the machine. (You can opt into sending a couple of
sanitized sample values with include_samples=True, but it is OFF by default.)

Provider — OpenAI-compatible
----------------------------
Works with any endpoint that speaks the OpenAI /chat/completions API: OpenAI,
Azure OpenAI, Together, Groq, or a local vLLM / Ollama / LM Studio server. Set
base_url + api_key + model. Uses only the Python standard library (urllib), so
there is no SDK dependency to install or pin.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import urllib.request
from typing import Callable, Optional

# No hardcoded field definitions — descriptions come from the config (each
# output field may carry a `description`). When a field has none, the matcher
# falls back to the field name itself, so this works for ANY domain, not just
# banking. Pass `field_defs={field: description}` to override.
FIELD_DEFS: dict[str, str] = {}

# Domain-neutral default. The JSON output contract lives in the USER message
# (always sent), so this can be safely overridden without breaking the format.
# Override per matcher (system_prompt=...), or per config (ai_system_prompt),
# or via TABULARMAPPER_AI_SYSTEM_PROMPT.
DEFAULT_SYSTEM_PROMPT = (
    "You map spreadsheet COLUMNS to a fixed schema. You are given only column "
    "headers and structural metadata (data types, fill rates, sign, and which "
    "columns are mutually exclusive) — never the actual cell values. Use the "
    "header wording, the field descriptions provided, and these structural hints. "
    "Two mutually-exclusive numeric columns are often a directional pair (e.g. "
    "debit/credit, in/out, paid/received); decide direction from the header "
    "wording. A single signed numeric column (has negative values, not mutually "
    "exclusive with another numeric column) is usually a single net amount. "
    "Respond with ONLY a JSON object mapping the column index (as a string) to "
    "one field name, or null if a column matches no field. Do not invent fields."
)


# --------------------------------------------------------------------------
# Structural profiling — deterministic, no cell contents leave this function
# --------------------------------------------------------------------------
def _classify(v) -> str:
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return "empty"
    if isinstance(v, (_dt.datetime, _dt.date)):
        return "date"
    if isinstance(v, bool):
        return "text"
    if isinstance(v, (int, float)):
        return "number"
    s = str(v).strip()
    if re.match(r"^[-(]?[\d,]+\.?\d*\)?\s*(dr|cr)?$", s, re.I):
        return "number"
    if re.search(r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}", s) or \
       re.match(r"\d{1,2}\s*[A-Za-z]{3,9}\s*\d{2,4}", s):
        return "date"
    return "text"


def _is_negative(v) -> bool:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v < 0
    if isinstance(v, str):
        s = v.strip().lower()
        return s.startswith("-") or ("(" in s and ")" in s) or s.endswith("dr")
    return False


def profile_columns(header_row: list, data_rows: list[list],
                    max_rows: int = 40) -> list[dict]:
    """Return a per-column STRUCTURAL profile — no raw cell values.

    Fields: index, name, dtype (majority), fill_rate, has_negative,
    mutually_exclusive_with (column indices never co-filled -> debit/credit
    pairs). This is exactly the signal a human uses to tell debit from credit
    without reading the numbers.
    """
    ncols = len(header_row)
    rows = data_rows[:max_rows]
    filled = [[False] * ncols for _ in rows]
    dtypes: list[list[str]] = [[] for _ in range(ncols)]
    neg = [False] * ncols

    for r_i, row in enumerate(rows):
        for c in range(ncols):
            v = row[c] if c < len(row) else None
            t = _classify(v)
            if t != "empty":
                filled[r_i][c] = True
                dtypes[c].append(t)
                if _is_negative(v):
                    neg[c] = True

    profiles = []
    for c in range(ncols):
        types = dtypes[c]
        majority = max(set(types), key=types.count) if types else "empty"
        fill_rate = (sum(1 for r in filled if r[c]) / len(rows)) if rows else 0.0
        # mutual exclusivity: never filled in the same row as column d
        excl = []
        for d in range(ncols):
            if d == c:
                continue
            both = any(r[c] and r[d] for r in filled)
            c_has = any(r[c] for r in filled)
            d_has = any(r[d] for r in filled)
            if c_has and d_has and not both:
                excl.append(d)
        profiles.append({
            "index": c,
            "name": ("" if header_row[c] is None else str(header_row[c]).strip()),
            "dtype": majority,
            "fill_rate": round(fill_rate, 2),
            "has_negative": neg[c],
            "mutually_exclusive_with": excl,
        })
    return profiles


# --------------------------------------------------------------------------
# OpenAI-compatible table matcher
# --------------------------------------------------------------------------
class OpenAICompatibleMatcher:
    """Map an unknown header row to output fields with one LLM call.

    Transport is any OpenAI-compatible /chat/completions endpoint. Inject a
    custom `transport` (messages -> assistant_text) to unit-test without network.
    """

    def __init__(self,
                 base_url: Optional[str] = None,
                 api_key: Optional[str] = None,
                 model: Optional[str] = None,
                 field_defs: Optional[dict] = None,
                 include_samples: bool = False,
                 timeout: float = 30.0,
                 temperature: float = 0.0,
                 system_prompt: Optional[str] = None,
                 transport: Optional[Callable[[list], str]] = None):
        self.base_url = (base_url or os.getenv("OPENAI_BASE_URL")
                         or "https://api.openai.com/v1").rstrip("/")
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        self.field_defs = field_defs if field_defs is not None else dict(FIELD_DEFS)
        self.include_samples = include_samples
        self.timeout = timeout
        self.temperature = temperature
        # System prompt: explicit arg > env > domain-neutral default. The user
        # message always carries the JSON-format contract, so overriding this is safe.
        self.system_prompt = (system_prompt
                              or os.getenv("TABULARMAPPER_AI_SYSTEM_PROMPT")
                              or DEFAULT_SYSTEM_PROMPT)
        self._transport = transport  # for tests / custom clients

    # -- prompt construction (structure only) --
    def _build_messages(self, profiles: list[dict], allowed_fields: list[str]) -> list:
        field_lines = "\n".join(
            f"  - {f}: {self.field_defs.get(f, f)}"
            for f in allowed_fields
        )
        col_lines = []
        for p in profiles:
            excl = (f", mutually-exclusive with columns {p['mutually_exclusive_with']}"
                    if p["mutually_exclusive_with"] else "")
            neg = ", contains negative values" if p["has_negative"] else ""
            col_lines.append(
                f"  [{p['index']}] name={p['name']!r} "
                f"type={p['dtype']} fill={p['fill_rate']}{neg}{excl}"
            )
        cols = "\n".join(col_lines)
        system = self.system_prompt
        user = (
            f"Allowed fields:\n{field_lines}\n\n"
            f"Columns:\n{cols}\n\n"
            "Return JSON like {\"0\": \"date\", \"1\": \"description\", "
            "\"4\": null}. Every column index must appear exactly once."
        )
        return [{"role": "system", "content": system},
                {"role": "user", "content": user}]

    # -- HTTP transport (stdlib) --
    def _http(self, messages: list) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "response_format": {"type": "json_object"},
        }
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return body["choices"][0]["message"]["content"]

    # -- parse + validate --
    @staticmethod
    def _parse(text: str, ncols: int, allowed_fields: list[str]) -> dict:
        m = re.search(r"\{.*\}", text, re.S)
        raw = json.loads(m.group(0) if m else text)
        # single-slot fields: keep only the first (highest-priority) assignment
        result: dict[int, str] = {}
        seen: set[str] = set()
        for k, v in raw.items():
            try:
                ci = int(k)
            except (ValueError, TypeError):
                continue
            if not (0 <= ci < ncols):
                continue
            if v in allowed_fields and v not in seen:
                result[ci] = v
                seen.add(v)
        return result

    def __call__(self, header_row: list, data_rows: list[list],
                 allowed_fields: list[str]) -> dict:
        """Return {col_index: field} for the header. Empty dict on any failure
        (caller then leaves those columns unmapped -> needs_review)."""
        profiles = profile_columns(header_row, data_rows)
        messages = self._build_messages(profiles, allowed_fields)
        try:
            text = self._transport(messages) if self._transport else self._http(messages)
        except Exception:  # noqa: BLE001 — network/parse errors must not crash the pipeline
            return {}
        try:
            return self._parse(text, len(header_row), allowed_fields)
        except (json.JSONDecodeError, ValueError, TypeError):
            return {}
