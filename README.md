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
python cli.py <input.xlsx> [output.xlsx] [options]
```

Prints the detected header row, the full column mapping with confidences and
method, transaction count, and any review flags. Options:

```
--format {file,json,bytes,base64,records}
                                      output format (default: file)
--ai                                  use the LLM table matcher for unknown
                                      headers (OpenAI-compatible; structure
                                      only, never transaction data)
--model NAME                          LLM model (or env OPENAI_MODEL)
--fallback {none,hashing}             offline per-column fallback
                                      (default: none -> zero network calls)
--config PATH                         output template + synonyms JSON
                                      (file / URL / s3://; or env BANK_MAPPER_CONFIG)
--no-cache                            disable mapping_cache.json
--threshold N                         fuzzy confidence gate (default 80)
```

For `--ai`, set `OPENAI_API_KEY` (and optionally `OPENAI_BASE_URL`,
`OPENAI_MODEL`). Works with OpenAI, Azure, Together, Groq, or a local
vLLM / Ollama / LM Studio server — anything OpenAI-compatible.

Examples:

```bash
# Original behavior — write .xlsx to disk
python cli.py "samples/PAYIR_FC_SBI_2025.xlsx"     # deterministic, no network

# JSON output to stdout
python cli.py statement.xlsx --format json

# Raw .xlsx bytes — pipe to file
python cli.py statement.xlsx --format bytes > out.xlsx

# Base64 encoded — embed in scripts
python cli.py statement.xlsx --format base64

# Records as JSON — inspect raw data
python cli.py statement.xlsx --format records

# With AI and custom threshold
export OPENAI_API_KEY=sk-...
python cli.py new_bank.xlsx out.xlsx --ai --format file
python cli.py new_bank.xlsx --fallback hashing --format json
```

## Output formats

The engine supports **five output formats** via the `output_format` parameter:

| Format | Type | Best for |
|--------|------|----------|
| `records` | `list[dict]` | In-memory processing, JSON APIs |
| `json` | `str` | HTTP JSON responses, logging, queues |
| `bytes` | `bytes` | `StreamingResponse`, file downloads, S3 upload |
| `base64` | `str` | Embedding in JSON, email attachments, data URIs |
| `file` | `str` (path) | CLI, batch jobs, saving to disk |

`process_file` defaults to `file`; `process_stream` defaults to `records`.

Library use:

```python
from bank_mapper import process_file, process_stream
from mapping_cache import MappingCache

# --- File input, file output (original behavior) ---
res = process_file("statement.xlsx", out_path="out.xlsx",
                   output_format="file", cache=MappingCache())
print(res.output.to_response())   # -> "out.xlsx"

# --- Stream input, JSON output (API endpoint) ---
with open("statement.xlsx", "rb") as f:
    res = process_stream(f.read(), output_format="json")
print(res.output.json)            # -> JSON string

# --- Stream input, bytes output (StreamingResponse) ---
res = process_stream(uploaded_bytes, output_format="bytes")
# res.output.bytes  ->  .xlsx bytes for StreamingResponse

# --- Stream input, base64 output (embedded payload) ---
res = process_stream(uploaded_bytes, output_format="base64")
payload = {"filename": "mapped.xlsx", "content_b64": res.output.base64}

# --- In-memory records, no serialization cost ---
res = process_stream(uploaded_bytes, output_format="records")
for rec in res.output.records:
    print(rec["date"], rec["debit"], rec["credit"])
```

`res.output` is an `OutputResult` with lazy-evaluated properties:
- `.records` — raw Python list (always available, no cost)
- `.json` — JSON string (cached on first access)
- `.bytes` — in-memory `.xlsx` bytes via `BytesIO` (cached)
- `.base64` — base64-encoded `.xlsx` bytes (cached, derived from `.bytes`)
- `.to_response()` — returns the native object for the requested format

For CSV output, use the standalone serializer:

```python
from bank_mapper import records_to_csv_bytes
csv_bytes = records_to_csv_bytes(res.records)
```

## Multi-output patterns

A common need: process once, then fan out to multiple destinations — e.g.
save JSON to a database, upload `.xlsx` bytes to S3, and emit a base64 payload
to an audit queue. You do **not** need multiple `process_stream` calls.

The extraction pass is the expensive part (parsing, header detection, column
mapping, date/amount normalization). Serialization is cheap by comparison.
`output_format="records"` defers all serialization costs until you access them.

### Pattern 1: DB + S3 + audit log (one pass, three destinations)

```python
from bank_mapper import process_stream, records_to_csv_bytes
from mapping_cache import MappingCache
import json

# ONE extraction pass — zero serialization work
res = process_stream(uploaded_bytes, output_format="records", cache=MappingCache())

records = res.records

# Destination 1: JSON → database
json_payload = json.dumps(records)
await db.insert_transactions(json_payload)

# Destination 2: .xlsx bytes → S3 (lazy, built once, cached)
xlsx_bytes = res.output.bytes
s3_client.put_object(Bucket="statements", Key="mapped/2024-01.xlsx", Body=xlsx_bytes)

# Destination 3: base64 → audit queue (reuses cached bytes above)
audit_log.emit({"file_b64": res.output.base64})

# Destination 4: CSV → legacy system (standalone, no caching)
csv_bytes = records_to_csv_bytes(records)
ftp.upload("report.csv", csv_bytes)
```

### Pattern 2: FastAPI — return JSON + async background upload

```python
from fastapi import UploadFile, BackgroundTasks
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
import boto3

from bank_mapper import process_stream
from mapping_cache import MappingCache

CACHE = MappingCache()
s3 = boto3.client("s3")

async def upload_statement(file: UploadFile, background: BackgroundTasks):
    data = await file.read()

    # Single pass
    res = await run_in_threadpool(
        process_stream, data,
        output_format="records", cache=CACHE
    )

    if res.needs_review:
        return JSONResponse(status_code=422, content={
            "needs_review": True, "reasons": res.review_reasons
        })

    # Return JSON immediately
    response = {"transactions": len(res.records), "records": res.records}

    # Background: upload .xlsx to S3 (doesn't block the response)
    background.add_task(
        s3.put_object,
        Bucket="statements",
        Key=f"mapped/{file.filename}",
        Body=res.output.bytes  # built lazily here, cached for reuse
    )

    return response
```

### Pattern 3: CLI pipe — bytes to file + JSON to stdout

```bash
# Extract once, pipe .xlsx to disk, capture JSON for logging
python cli.py statement.xlsx --format bytes > out.xlsx
python cli.py statement.xlsx --format json > mapping_log.json

# Or in a single script:
python -c "
import json, sys
from bank_mapper import process_file

res = process_file('statement.xlsx', output_format='records')

# Save .xlsx
with open('out.xlsx', 'wb') as f:
    f.write(res.output.bytes)

# Save JSON metadata
with open('meta.json', 'w') as f:
    json.dump({
        'header_index': res.header_index,
        'transactions': len(res.records),
        'needs_review': res.needs_review,
        'column_maps': [
            {'header': m.raw_header, 'field': m.field, 'conf': m.confidence}
            for m in res.column_maps
        ]
    }, f, indent=2)
"
```

### Cost breakdown

| Step | What happens | Cost |
|------|-------------|------|
| `process_stream(..., output_format="records")` | Parse xlsx, detect header, map columns, normalize dates/amounts | **One-time, expensive** |
| `res.records` | Return the already-built list | Free |
| `res.output.json` | `json.dumps(records)` | Cheap, cached after first call |
| `res.output.bytes` | `openpyxl` → `BytesIO` | Cheap, cached after first call |
| `res.output.base64` | `base64.b64encode(bytes)` | Cheap, derived from cached bytes |
| `records_to_csv_bytes(records)` | `csv.DictWriter` → `BytesIO` | Cheap, no caching |

### Rule of thumb

| Scenario | Use |
|----------|-----|
| One destination, format known upfront | `output_format="json"` or `"bytes"` or `"file"` (eager, clean) |
| Multiple destinations, same data | `output_format="records"` (lazy, fan-out) |
| Need both raw data + serialized file | `output_format="records"` → access `.records` + `.bytes` |

## Output schema & config (`schema.py`)

The output template and the synonym vocabulary are **configuration, not code**.
By default they match `result-template.xlsx` plus balance:

```
Date | Narration | Reference Number | Debit | Credit | Balance
```

- `date` → `YYYY-MM-DD` (year-first formats are never day/month-flipped).
- `debit` / `credit` → positive floats on the correct side. A single signed
  `Amount` column is split: negative → debit, positive → credit.

### Change the template without touching code

Load a config JSON from a file, an HTTP(S) URL, an S3 object, or a dict — see
`config.example.json` for the shape:

```python
from bank_mapper import configure
configure("config.json")                                  # local file
configure("https://cdn.example.com/bank-config.json")     # any URL (stdlib)
configure("s3://my-bucket/bank-config.json")              # S3 (presigned URL or boto3)
# or set the env var once:  BANK_MAPPER_CONFIG=./config.json
```

Each output column is `{"field", "header", "type"}` where `type` ∈
`date | money | text`. Renaming a header, reordering, dropping a column, or
**adding a brand-new column** (e.g. a `value_date`) is a JSON edit — the new
field is extracted generically by its `type`. The field keys `debit`, `credit`,
`amount` keep their special reconciliation behavior. On a bad/unreachable config
source the loader falls back to the built-in defaults, so the service never dies
on a config typo. Defaults are byte-identical to the previous hardcoded values
(verified by the test suite).

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

No code changes, no retraining. But you usually won't do even this by hand —
the self-learning loop grows the vocabulary for you (below).

## Self-learning vocabulary (`learn.py`)

The system teaches itself so you stop editing synonyms at all. When the AI
resolves a new bank's header, that phrase is written to a **learn store** and
becomes a deterministic **exact** match from then on — the AI never fires for it
again. See `docs/self-learning-synonyms.md` for the full design.

```python
from learn import LearnStore
from bank_mapper import apply_learned, process_file

store = LearnStore()                 # BANK_MAPPER_LEARN_STORE (sqlite/redis/…) or sqlite default
apply_learned(store)                 # activate learned synonyms at startup
res = process_file("stmt.xlsx", table_matcher=matcher, learn_store=store)  # processes + learns
```

Trust policy (financial-data safe by default): `date` / `description` /
`reference` / `balance` auto-apply; **`debit` / `credit` are gated to a review
queue**, because a wrong debit/credit direction is the one costly error.

```python
store.pending()                      # gated phrases awaiting a human
store.approve("outgoing", "debit")   # -> exact match everywhere afterward
store.reject("incoming", "credit")
```

Effective vocabulary = seed (config) + learned, seed authoritative on conflict.
In the API this is automatic: the router learns on every `/map` and exposes
`GET/POST /statements/learn/{pending,approve,reject}`. Set
`LearnStore(auto_apply_gated=True)` for a fully unattended loop.

**Bootstrap from your archive.** Seed the vocabulary in one pass from statements
you already have (`harvest_folder`, or the CLI):

```bash
python cli.py --harvest ./past_statements --learn sqlite:///learned.db
# add --ai to also resolve headers fuzzy can't place
```

It runs the mapper over every `.xlsx`, learns each confident non-exact header,
and queues debit/credit for a one-time review. Then those banks map exactly.

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

## Use it from your FastAPI backend

### Install it as a dependency

The project is a proper installable package. From your backend's environment:

```bash
# from a local checkout
uv add /path/to/bank-statement-mapper[api]
# or from the (private) git repo — needs a token / SSH access
uv add "bank-statement-mapper[api] @ git+https://github.com/KarthiKeyan05046/bank-statement-mapper.git"
# plain pip works too
pip install "/path/to/bank-statement-mapper[api]"
```

The `[api]` extra pulls `fastapi` + `python-multipart`. Omit it if you only want
the library (`process_file`) and not the router.

### Option A — mount the ready-made router (fastest)

`bank_mapper_api.py` ships an `APIRouter` plus a `lifespan` that builds the cache
and AI matcher once at startup:

```python
from fastapi import FastAPI
from bank_mapper_api import router, lifespan

app = FastAPI(lifespan=lifespan)     # or merge into your existing lifespan
app.include_router(router)
# -> POST /statements/map   (upload .xlsx)   GET /statements/health
```

Run standalone to try it: `uvicorn bank_mapper_api:app --reload`.

### Option B — call the library yourself

If you want full control of the endpoint, use `process_stream` — it reads the
upload's **raw bytes in memory, with no temp file** (nothing hits disk, which
matters for bank data). Build `MappingCache` + `OpenAICompatibleMatcher` **once**
and run the blocking call in a threadpool:

```python
from fastapi import UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse, JSONResponse
import io

from bank_mapper import process_stream, records_to_csv_bytes
from mapping_cache import MappingCache
from ai_matcher import OpenAICompatibleMatcher

CACHE = MappingCache()
MATCHER = OpenAICompatibleMatcher()          # reads OPENAI_* env; None-safe if no key

async def handle_json(file: UploadFile):
    """Return mapped records as JSON."""
    data = await file.read()
    res = await run_in_threadpool(
        process_stream, data,
        output_format="json",
        table_matcher=MATCHER, cache=CACHE
    )
    if res.needs_review:
        return JSONResponse(
            status_code=422,
            content={"needs_review": True, "reasons": res.review_reasons}
        )
    return JSONResponse(content=json.loads(res.output.json))

async def handle_xlsx(file: UploadFile):
    """Return mapped records as a downloadable .xlsx file."""
    data = await file.read()
    res = await run_in_threadpool(
        process_stream, data,
        output_format="bytes",
        table_matcher=MATCHER, cache=CACHE
    )
    if res.needs_review:
        return JSONResponse(
            status_code=422,
            content={"needs_review": True, "reasons": res.review_reasons}
        )
    return StreamingResponse(
        io.BytesIO(res.output.bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=mapped.xlsx"}
    )

async def handle_base64(file: UploadFile):
    """Return mapped records as base64-encoded .xlsx (for embedding)."""
    data = await file.read()
    res = await run_in_threadpool(
        process_stream, data,
        output_format="base64",
        table_matcher=MATCHER, cache=CACHE
    )
    return JSONResponse(content={
        "needs_review": res.needs_review,
        "filename": "mapped.xlsx",
        "content_b64": res.output.base64
    })
```

`process_stream` accepts raw `bytes` or any binary file-like (e.g. `file.file`).
Use `process_file(path, ...)` when you already have an .xlsx on disk. The shipped
router (Option A) uses `process_stream`, so uploads never touch the filesystem.

### Full flow: store the file to S3 + insert rows into your DB

The mapper deliberately stays out of AWS and your database — it just returns
`res.records` (a list of plain dicts, ready to insert). Your endpoint owns S3 and
the DB, and both use the *same* upload bytes, so the file is read only once:

```python
import boto3, uuid
from fastapi import UploadFile
from fastapi.concurrency import run_in_threadpool
from bank_mapper import process_stream
from mapping_cache import MappingCache
from ai_matcher import OpenAICompatibleMatcher

s3 = boto3.client("s3")
CACHE = MappingCache()
MATCHER = OpenAICompatibleMatcher()          # None-safe if OPENAI_API_KEY unset

async def ingest(file: UploadFile):
    data = await file.read()                 # read ONCE, in memory

    # 1) store the original file to S3 (your bucket, your key scheme)
    key = f"statements/{uuid.uuid4()}/{file.filename}"
    await run_in_threadpool(s3.put_object, Bucket="my-bucket", Key=key, Body=data)

    # 2) parse to JSON rows (no temp file)
    res = await run_in_threadpool(process_stream, data,
                                  table_matcher=MATCHER, cache=CACHE)

    # 3) insert rows into your DB — res.records is already JSON-shaped
    rows = [{**r, "s3_key": key} for r in res.records]   # tag each row with the file
    await your_db.insert_many("transactions", rows)

    return {"s3_key": key, "count": len(rows), "needs_review": res.needs_review}
```

Each record is `{date, description, reference, debit, credit, balance}` — ISO date
strings, floats or `null`. Tag rows with the `s3_key` (as above) so every
transaction links back to its source file in S3. If a statement comes back
`needs_review=True`, quarantine it (store the file, hold the rows) instead of
inserting blindly.

Notes for production:
- Omit `table_matcher` for a strict, model-free service — unknown headers then
  just set `needs_review=True`.
- `process_file` is synchronous (openpyxl + a possible blocking LLM call). Always
  run it in a threadpool (Option A does this for you) so it doesn't stall the
  event loop; for large files or high volume, move it to a background job.
- The `MappingCache` backend is URL-selected and defaults to **SQLite**
  (concurrency-safe). For multiple containers on separate hosts, point
  `BANK_MAPPER_CACHE` at `redis://…` or `postgresql://…` so they share one cache.
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

## Mapping cache (pluggable storage)

The cache stores `{header_fingerprint → column mapping}` so a repeat bank layout
skips detection/mapping entirely (true 100% on seen formats). The fingerprint is
a hash of the normalized header strings, independent of row content. Pass
`cache=MappingCache()` (the CLI does this unless `--no-cache`).

The **backend is chosen by a URL** (`stores.open_store`), the same way you'd
point SQLAlchemy or Celery at a database — swap it with an env var, no code:

```
BANK_MAPPER_CACHE=memory://                  # tests / single worker
BANK_MAPPER_CACHE=sqlite:///mapping_cache.db # DEFAULT — file, no server, concurrency-safe
BANK_MAPPER_CACHE=redis://localhost:6379/0   # multi-worker      (pip install ...[redis])
BANK_MAPPER_CACHE=valkeys://user:pw@host/0   # Valkey e.g. Aiven (pip install ...[valkey])
BANK_MAPPER_CACHE=postgresql://user@host/db  # shared, durable   (pip install ...[postgres])
```

```python
MappingCache()                               # env BANK_MAPPER_CACHE, else sqlite
MappingCache("redis://localhost:6379/0")     # explicit
MappingCache(path="legacy.json")             # legacy JSON file (back-compat)
```

The default is **SQLite**, not a JSON file: still a single file with no server to
run, but with real transactions and locking, so it's safe under multiple workers
(the old JSON cache raced). Any object with `get()`/`put()` also works — inject
your own store for a backend not shipped here. The learned-synonyms store (next)
uses this exact same convention.

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
                   OutputResult (records/json/bytes/base64/file), CSV serializer
ai_matcher.py      OpenAICompatibleMatcher + profile_columns (AI table matcher)
llm_fallback.py    HashingEmbeddingFallback / make_llm_fallback (offline fallback)
bank_mapper_api.py FastAPI router (POST /statements/map) + standalone app
mapping_cache.py   MappingCache (header fingerprint → mapping)
cli.py             command-line runner with --format flag
make_fixtures.py   regenerates test_statements/
samples/           real bank statements for manual runs
test_statements/   synthetic pytest fixtures
tests/             pytest suite
```

## Scope (v1)

xlsx only. Library + CLI (no web UI / DB). No transaction categorization.
The `02/06` day-vs-month ambiguity is resolved per-locale (default day-first for
non-US banks) and surfaced, never silently guessed.
