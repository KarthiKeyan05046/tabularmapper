# Bank Statement → Standard Schema Mapper

Take a bank statement `.xlsx` from **any bank, in any layout**, and produce a
standardized `.xlsx` with fixed columns — mapping done reliably, every
unrecognized format flagged for human review.

The design is deliberately split so the common path is **100% deterministic**
and only genuinely ambiguous columns are routed to a model (or a human):

```
Stage 1  detect_header_row()   deterministic scoring — finds the real header
                               row even under bank logos / metadata (NO AI)
Stage 2  map_columns()         exact synonym → fuzzy → optional local fallback
Then     extract_records()     deterministic date/amount parsing, debit/credit
                               reconciliation (a model NEVER sees a data row)
```

## Why it's trustworthy

- **No model ever touches transaction data.** The fallback sees only a column
  header string + up to 3 sample cells + the allowed field names. All dates,
  amounts and row logic are plain Python — auditable and private.
- **Header detection is scoring, not AI.** No model call to locate the header.
- **Fallback is off by default.** `llm_fallback=None` → **zero network calls**.
- **Human-review gate.** Missing date/money column, low-confidence, or
  fallback-resolved columns set `needs_review=True` with reasons. Financial
  data is never silently best-guessed.
- **Every column decision is logged** with method (`exact`/`fuzzy`/`llm`/`cache`)
  and 0–100 confidence.

## Install

```bash
pip install -e .    # core engine (openpyxl, rapidfuzz, python-dateutil)
pip install pytest  # for tests
```

No SDK to add for the AI path — the LLM matcher uses only the standard library.
The core install makes **zero network calls**; the AI only fires when you pass
`--ai` (or a `table_matcher`) and only for a header the synonyms can't place.

## Run

```bash
python cli.py <input.xlsx> [output.xlsx]
```

Prints the detected header row, the full column mapping with confidences and
method, transaction count, and any review flags. Options:

```
--ai                      use the LLM table matcher for unknown headers
--model NAME              LLM model (or env OPENAI_MODEL)
--fallback {none,hashing} offline per-column fallback (default none)
--no-cache                disable mapping_cache.json
--threshold N             fuzzy confidence gate (default 80)
```

For `--ai`, set `OPENAI_API_KEY` (and optionally `OPENAI_BASE_URL`,
`OPENAI_MODEL`). Works with OpenAI, Azure, Together, Groq, or a local
vLLM / Ollama / LM Studio server — anything OpenAI-compatible.

Examples:

```bash
python cli.py "samples/PAYIR_FC_SBI_2025.xlsx"     # deterministic, no network
export OPENAI_API_KEY=sk-...
python cli.py new_bank.xlsx out.xlsx --ai          # AI maps a new layout, then caches it
python cli.py new_bank.xlsx --fallback hashing     # offline lexical, no API
```

Library use:

```python
from bank_mapper import process_file
from mapping_cache import MappingCache

res = process_file("statement.xlsx", out_path="out.xlsx", cache=MappingCache())
print(res.header_index, res.needs_review, res.review_reasons)
for m in res.column_maps:
    print(m.raw_header, "→", m.field, m.confidence, m.method)
```

## Output schema

Edit the single constant `OUTPUT_SCHEMA` in `bank_mapper.py`. Default mirrors
`result-template.xlsx` plus balance:

```
Date | Narration | Reference Number | Debit | Credit | Balance
```

- `date` → `YYYY-MM-DD` (year-first formats are never day/month-flipped).
- `debit` / `credit` → positive floats on the correct side. A single signed
  `Amount` column is split: negative → debit, positive → credit.

## Add a new bank in under 5 minutes

Everything keys off the `SYNONYMS` dict in `bank_mapper.py`
(`target field → list of header phrases`). To support a new bank:

1. Open its statement, note the header cell names (e.g. `"Paid Out"`,
   `"Value Dt"`).
2. Add each phrase (lower-cased) to the matching field's list:

   ```python
   SYNONYMS["debit"].append("paid out")
   SYNONYMS["date"].append("value dt")
   ```

3. Re-run. Exact matches score 100; near-misses are caught by fuzzy at ≥80.

No code changes, no retraining. If a header is too novel for synonyms + fuzzy,
the AI table matcher handles it automatically.

## The AI table matcher (new banks, no manual work)

When a statement's header is unknown to the synonym table, one LLM call maps the
**whole header row** to the output fields, and the result is written straight
into `mapping_cache.json`. So a brand-new bank costs a single AI call — after
that it's cached and mapped instantly, forever, with no model call.

```
exact synonym match   known banks — free, instant, 100% confidence
   ↓  (only if a critical field is unmatched)
AI table matcher      reads headers + structural profile, returns a full mapping
   ↓
cache the result      that layout is now "known" → never hits the AI again
```

### It matches the table, never your data

The prompt the model receives contains **only**:

- the column header strings (e.g. `"Withdrawals"`, `"Value Dt"`)
- a **structural profile per column**, computed locally: data type, fill rate,
  whether it holds negatives, and which columns are *mutually exclusive*
- the list of allowed output fields + short descriptions

It never contains a transaction amount, date, name, narration, or reference —
no real statement data leaves the machine. (The mutual-exclusivity signal is how
the model tells a debit/credit pair apart without ever seeing the numbers.) This
is enforced by a test (`test_ai_matcher_sends_no_real_data`).

### Provider — OpenAI-compatible, swappable

`ai_matcher.OpenAICompatibleMatcher` talks to any `/chat/completions` endpoint
using only the standard library (no SDK dependency). Point it wherever you like:

```python
from bank_mapper import process_file
from ai_matcher import OpenAICompatibleMatcher

matcher = OpenAICompatibleMatcher(              # reads OPENAI_API_KEY / _BASE_URL / _MODEL
    base_url="https://api.openai.com/v1",       # or Azure / Together / local vLLM / Ollama
    model="gpt-4o-mini",
)
res = process_file("new_bank.xlsx", table_matcher=matcher, cache=MappingCache())
```

AI-assigned columns get `method="ai"`, confidence 85, and are trusted (no forced
review) so the pipeline runs unattended. If the AI still can't place a critical
field — or the API is unreachable — the statement falls back to `needs_review`
instead of guessing. Nothing crashes on an API error.

> One honest caveat: a capable model reads the header wording + mutual-exclusivity
> and gets debit/credit right the vast majority of the time — but no model is
> 100%. For financial data, the safe pattern is to glance at the **first**
> statement from each new bank (it's cached after), or set a stricter policy in
> `evaluate_review`. The cache means that check happens at most once per bank.

## FastAPI integration

The mapper is a plain library — import `process_file`. Build the matcher and
cache **once** at startup and reuse them across requests:

```python
# app.py
import tempfile, os
from fastapi import FastAPI, UploadFile, File
from bank_mapper import process_file, OUTPUT_SCHEMA
from mapping_cache import MappingCache
from ai_matcher import OpenAICompatibleMatcher

app = FastAPI()
CACHE = MappingCache()                       # persistent header cache
MATCHER = OpenAICompatibleMatcher()          # reads OPENAI_* env, built once

@app.post("/map-statement")
async def map_statement(file: UploadFile = File(...)):
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        tmp.write(await file.read())
        path = tmp.name
    try:
        res = process_file(path, table_matcher=MATCHER, cache=CACHE)
    finally:
        os.unlink(path)
    return {
        "header_index": res.header_index,
        "needs_review": res.needs_review,
        "review_reasons": res.review_reasons,
        "columns": [
            {"raw": m.raw_header, "field": m.field,
             "confidence": m.confidence, "method": m.method}
            for m in res.column_maps
        ],
        "transactions": res.records,          # already YYYY-MM-DD + split debit/credit
        "schema": [disp for _, disp in OUTPUT_SCHEMA],
    }
```

Notes for production:
- Omit `table_matcher` for a strict, model-free service — unknown headers then
  just set `needs_review=True`.
- The `MappingCache` means a bank format seen once maps instantly thereafter,
  with no AI call.
- `res.records` is JSON-ready; write to xlsx with `process_file(..., out_path=...)`
  only if you need a file artifact.

## Human-review workflow

When `needs_review=True`, the result carries `review_reasons`, e.g.:

```
- missing critical field: date
- no debit/credit or signed amount column found
- low-confidence column 'Outgoing' → debit (70, llm)
- fallback-resolved column 'Incoming' → credit
```

With `--ai`, most new banks are mapped automatically and never reach this queue.
Review remains the safety net for when the AI can't place a critical field or the
API is unreachable. A reviewer confirms/corrects the mapping, then either adds the
header phrases to `SYNONYMS` or trusts the cache — after which that format maps
with no review or AI call.

## Mapping cache

`mapping_cache.json` stores `{header_fingerprint → column mapping}`. A repeat
bank layout skips detection/mapping entirely (true 100% on seen formats). The
fingerprint is a hash of the normalized header strings, so it's independent of
row content. Pass `cache=MappingCache()` (the CLI does this unless `--no-cache`).

## Tests

```bash
pytest -q
```

`tests/test_mapper.py` asserts, per fixture: correct header index, correct
field-per-column, correct debit/credit split, correct normalized dates, and the
right `needs_review` value — plus normalizer unit tests, an offline-fallback
test, and a guard that `fallback=None` makes no socket calls.

Fixtures live in `test_statements/` (regenerate with `python make_fixtures.py`):

| File | Scenario |
|---|---|
| `01_junk_split.xlsx` | metadata rows on top + separate Debit/Credit |
| `02_title_signed.xlsx` | title row + single signed Amount column |
| `03_header_row1_brackets.xlsx` | header on row 1 + `(299)` bracket negatives |
| `04_garbage.xlsx` | unknown format → must trip `needs_review` |
| `05_weird_header.xlsx` | money columns only resolvable via the AI/fallback |

The AI matcher is tested with a mocked transport (no network): mapping, one-time
caching, graceful API-error handling, and the no-real-data-leaks guarantee.

## Layout

```
bank_mapper.py     engine: detect_header_row, map_columns, normalizers,
                   extract_records, process_file, merge_ai_mapping, SYNONYMS
ai_matcher.py      OpenAICompatibleMatcher + profile_columns (AI table matcher)
llm_fallback.py    HashingEmbeddingFallback / make_llm_fallback (offline fallback)
mapping_cache.py   MappingCache (header fingerprint → mapping)
cli.py             command-line runner
make_fixtures.py   regenerates test_statements/
samples/           real bank statements for manual runs
test_statements/   synthetic pytest fixtures
tests/             pytest suite
```

## Scope (v1)

xlsx only. Library + CLI (no web UI / DB). No transaction categorization.
The `02/06` day-vs-month ambiguity is resolved per-locale (default day-first for
non-US banks) and surfaced, never silently guessed.
