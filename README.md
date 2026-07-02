# Bank Statement Mapper

Turn a bank statement `.xlsx` from **any bank, in any layout** into a clean, fixed
schema — reliably, and with anything ambiguous flagged for review instead of
silently guessed.

The common path is **100% deterministic** (header detection + a synonym/fuzzy
matcher). An LLM is optional, off by default, and only ever sees column *headers*
+ column *structure* — never your transaction data.

```python
from bank_statement_mapper import process_file

res = process_file("statement.xlsx")
res.records         # -> [{'date': '2025-04-03', 'description': 'SALARY',
                    #      'reference': 'REF1', 'debit': None, 'credit': 45000.0,
                    #      'balance': None}, ...]  ready for JSON / a DB
res.needs_review    # -> False  (True if a column was uncertain)
```

**Contents:** [Install](#install) · [Quickstart](#quickstart) · [The result](#the-result-object)
· [Configuration](#configuration-env-vars) · [Storage backends](#storage-backends)
· [FastAPI](#use-with-fastapi) · [Output formats](#output-formats) · [AI matcher](#ai-column-matcher-optional)
· [Self-learning](#self-learning) · [Custom schema](#custom-output-schema) · [API reference](#api-reference)
· [Gotchas](#gotchas--faq)

---

## Install

```bash
pip install bank-statement-mapper                 # core — no DB driver, no AI SDK
pip install "bank-statement-mapper[api]"          # + FastAPI router
pip install "bank-statement-mapper[valkey]"       # + Valkey  (also: redis, postgres, dotenv)
```

The core install pulls only `openpyxl`, `rapidfuzz`, `python-dateutil`. Everything
else (Redis/Valkey/Postgres drivers, FastAPI, dotenv) is an **optional extra** you
add only if you use it. Import name is `bank_statement_mapper`.

## Quickstart

### 1. As a library

```python
from bank_statement_mapper import process_file, process_stream

# from a file path
res = process_file("statement.xlsx")
rows = res.records                       # list[dict], one per transaction

# from bytes (e.g. an upload) — parsed in memory, nothing written to disk
res = process_stream(open("statement.xlsx", "rb").read())
```

### 2. From the command line

```bash
bank-mapper statement.xlsx                    # writes statement.standardized.xlsx
bank-mapper statement.xlsx --format json      # prints JSON to stdout
bank-mapper statement.xlsx --format records   # prints the mapping + rows
```

### 3. In a FastAPI app

```python
from fastapi import FastAPI
from bank_statement_mapper.bank_mapper_api import router, lifespan

app = FastAPI(lifespan=lifespan)              # lifespan wires cache + config + AI for you
app.include_router(router)                    # -> POST /statements/map, GET /statements/health
```

That's the whole integration. **Do not add your own cache manager** — the
`lifespan` already builds the cache (see [Gotchas](#gotchas--faq)).

## How it works

```
1. detect header row   deterministic scoring — finds the real header even under
                       bank logos / metadata rows (never assumes row 1)
2. map columns         exact synonym → fuzzy match → optional AI (unknown headers)
3. extract rows        deterministic date/amount parsing, debit/credit vs signed
                       amount reconciliation (a model never sees a data row)
4. review gate         missing/uncertain critical column -> needs_review = True
```

## The result object

`process_file` / `process_stream` return a `ProcessResult`:

| Attribute | Type | What it is |
|---|---|---|
| `records` | `list[dict]` | the transactions — keys are your schema fields. **Use this for a DB.** |
| `needs_review` | `bool` | `True` if any critical column was missing or low-confidence |
| `review_reasons` | `list[str]` | human-readable reasons when `needs_review` |
| `column_maps` | `list[ColumnMap]` | per-column: `raw_header`, `field`, `confidence`, `method` |
| `header_index` | `int` | 0-based row where the header was found |
| `output` | `OutputResult` | serializers: `.records` `.json` `.bytes` `.base64` (see [formats](#output-formats)) |

```python
res = process_file("statement.xlsx")
if res.needs_review:
    print("review:", res.review_reasons)     # quarantine instead of trusting
else:
    db.insert_many(res.records)               # each dict is one row
```

## Configuration (env vars)

Everything swappable is set by an environment variable — **no code changes**.
All are optional; sensible defaults apply.

| Variable | Default | Purpose |
|---|---|---|
| `BANK_MAPPER_CACHE` | `sqlite:///mapping_cache.db` | where learned bank-layout mappings are cached ([backends](#storage-backends)) |
| `BANK_MAPPER_LEARN_STORE` | `sqlite:///learned_synonyms.db` | where self-learned header synonyms live |
| `BANK_MAPPER_CONFIG` | built-in | output template + synonyms JSON (file / `https://` / `s3://`) |
| `OPENAI_API_KEY` | *(unset → AI off)* | enables the AI column matcher |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | any OpenAI-compatible endpoint |
| `OPENAI_MODEL` | `gpt-4o-mini` | model name |

```bash
export BANK_MAPPER_CACHE="valkeys://default:PASSWORD@host:6379"
```

## Storage backends

The cache and the learn store share one URL convention (like SQLAlchemy/Celery) —
change the backend by changing the URL, nothing else:

| URL | Backend | Install |
|---|---|---|
| `memory://` | in-process (tests, single worker) | — |
| `sqlite:///cache.db` | SQLite file, concurrency-safe **(default)** | — |
| `redis://` / `rediss://` | Redis | `pip install "bank-statement-mapper[redis]"` |
| `valkey://` / `valkeys://` | Valkey (Redis fork, e.g. Aiven) | `pip install "bank-statement-mapper[valkey]"` |
| `postgresql://` | Postgres | `pip install "bank-statement-mapper[postgres]"` |

```python
from bank_statement_mapper import MappingCache, process_file

cache = MappingCache("valkeys://default:pw@host:6379")   # or MappingCache() to read the env var
res = process_file("statement.xlsx", cache=cache)
```

`MappingCache` is **synchronous** — `.get()`, `.put()`, `.close()`. There is no
async manager and no `init_cache`/`close_cache`. Selecting the backend is the URL,
full stop.

### Runtime files (the `.db`, `.db-wal`, `.db-shm` you may see)

With the default SQLite backend, the FastAPI `lifespan` opens the cache and the
learn store **at startup**, so these appear in the working directory before you
process anything:

```
mapping_cache.db        learned_synonyms.db        # the two SQLite databases
mapping_cache.db-wal    learned_synonyms.db-wal     # SQLite write-ahead log
mapping_cache.db-shm    learned_synonyms.db-shm     # SQLite shared-memory index
```

The `-wal` / `-shm` files are normal SQLite Write-Ahead-Logging sidecars (WAL is
what makes the file concurrency-safe); they're checkpointed away on a clean
shutdown. All of them are already in `.gitignore`. To control this:

```bash
BANK_MAPPER_CACHE=memory://           # no files at all (state lost on restart)
BANK_MAPPER_LEARN_STORE=memory://
# or relocate:
BANK_MAPPER_CACHE=sqlite:////var/lib/bankmapper/cache.db
# or use a server (no local files):
BANK_MAPPER_LEARN_STORE=valkeys://user:pw@host:6379
```

Use `memory://` for stateless / read-only-filesystem deployments; a path or a
Redis/Valkey/Postgres URL when you want the cache + learned vocabulary to survive
restarts.

## Use with FastAPI

The package ships a ready router. Two ways to use it.

### Simplest — use the built-in lifespan

```python
from fastapi import FastAPI
from bank_statement_mapper.bank_mapper_api import router, lifespan

app = FastAPI(lifespan=lifespan)
app.include_router(router)
```

At startup the `lifespan` reads `BANK_MAPPER_CONFIG`, builds `MappingCache()` from
`BANK_MAPPER_CACHE`, builds the learn store, and enables the AI matcher if
`OPENAI_API_KEY` is set. **Configure it entirely with env vars.**

### Control the cache yourself — write your own lifespan

```python
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
import bank_statement_mapper.bank_mapper as bank_mapper
from bank_statement_mapper.bank_mapper_api import router, state, build_matcher
from bank_statement_mapper import MappingCache, LearnStore, apply_learned

@asynccontextmanager
async def lifespan(app: FastAPI):
    bank_mapper.configure(os.getenv("BANK_MAPPER_CONFIG"))
    state.cache = MappingCache("valkeys://default:pw@host:6379")   # your URL
    state.matcher = build_matcher()          # None if no OPENAI_API_KEY
    state.learn = LearnStore()
    apply_learned(state.learn)
    yield
    state.cache.close()                      # sync, no await
    state.learn.close()

app = FastAPI(lifespan=lifespan)
app.include_router(router)
```

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/statements/map` | upload an `.xlsx`, get the mapping + rows (JSON) |
| `GET` | `/statements/health` | `{status, ai_enabled}` |
| `GET` | `/statements/learn/pending` | debit/credit synonyms awaiting approval |
| `POST` | `/statements/learn/approve` | approve a pending synonym (`?phrase=&field=`) |
| `POST` | `/statements/learn/reject` | reject a pending synonym |

`POST /statements/map` reads the upload in memory (no temp file) and runs the
blocking work in a threadpool. Store the original file to S3 in your own endpoint
if you need it — the mapper stays out of AWS.

## Output formats

`res.output` serializes the same records five ways, lazily (built once, cached):

| `output_format` | `res.output` accessor | Best for |
|---|---|---|
| `records` | `.records` (`list[dict]`) | DB insert, JSON APIs *(default for `process_stream`)* |
| `json` | `.json` (`str`) | HTTP responses, queues |
| `bytes` | `.bytes` (`bytes`) | `StreamingResponse`, S3 upload |
| `base64` | `.base64` (`str`) | embedding in JSON |
| `file` | writes to `out_path` | disk *(default for `process_file`)* |

```python
res = process_stream(data, output_format="records")
db.insert_many(res.records)                 # to your database
s3.put_object(Bucket=b, Key=k, Body=res.output.bytes)   # .xlsx to S3, one pass
```

CSV: `from bank_statement_mapper import records_to_csv_bytes`.

## AI column matcher (optional)

For a brand-new bank whose headers the synonyms can't place, one LLM call maps the
whole header row. It's **off unless `OPENAI_API_KEY` is set**, and the prompt
contains only column headers + structural metadata (types, fill rate, which
columns are mutually exclusive) — **never a transaction value**.

```python
from bank_statement_mapper.ai_matcher import OpenAICompatibleMatcher
res = process_file("new_bank.xlsx", table_matcher=OpenAICompatibleMatcher())
```

Works with OpenAI, Azure, Together, Groq, or a local vLLM/Ollama endpoint via
`OPENAI_BASE_URL`.

## Self-learning

When the AI resolves a new header, it's remembered so the next statement from that
bank maps deterministically (an `exact` match) with no AI call. Debit/credit are
held for a one-time human approval (a wrong direction is the costly error);
everything else auto-applies.

```python
from bank_statement_mapper import LearnStore, apply_learned, process_file
from bank_statement_mapper.ai_matcher import OpenAICompatibleMatcher

store = LearnStore()                         # BANK_MAPPER_LEARN_STORE or sqlite
apply_learned(store)                         # activate at startup
res = process_file("stmt.xlsx", table_matcher=OpenAICompatibleMatcher(), learn_store=store)

store.pending()                              # debit/credit awaiting review
store.approve("outgoing", "debit")           # now an exact match everywhere
```

Bootstrap from an archive in one pass:
`bank-mapper --harvest ./past_statements --learn sqlite:///learned.db`.

## Custom output schema

The output columns and synonyms are data, not code. Point `BANK_MAPPER_CONFIG` at
a JSON file (or `https://` / `s3://` URL):

```json
{
  "output_schema": [
    {"field": "date",        "header": "Date",   "type": "date"},
    {"field": "description", "header": "Details", "type": "text"},
    {"field": "debit",       "header": "Debit",  "type": "money"},
    {"field": "credit",      "header": "Credit", "type": "money"}
  ],
  "synonyms": { "debit": ["withdrawal", "paid out"] }
}
```

`type` is `date` | `number` (alias `money`) | `text`. Rename a header, reorder,
drop a column, or add a brand-new one — all config, no code. In a library (not
the CLI/API) call `configure("config.json")` before processing.

### Beyond banks — this is a general table→schema mapper

The engine has **no hardcoded field names**. "Bank" behavior is just the default
config; provide your own and it maps *any* spreadsheet (invoices, product
catalogs, payroll…). Optional keys, all data-driven (omit them for a plain
type-based mapping):

| Key | What it does |
|---|---|
| `output_schema[].description` | hint for the AI matcher (falls back to the field name) |
| `critical_fields` | fields that must be mapped, else `needs_review` |
| `require_any` | `[[a, b]]` — each group needs ≥1 mapped field, else `needs_review` |
| `reconcile` | `{"signed": s, "negative": n, "positive": p}` — split one signed column into two directional ones (the bank debit/credit rule) |
| `row_keep_if_any` | a row is a record only if ≥1 of these has a value (default: any non-empty) |
| `continuation_field` | a row with only this field folds into the row above (multi-line cells) |
| `replace_synonyms` | `true` to start from an empty vocabulary instead of extending the defaults |

The full bank preset is in `config.example.json` — copy it as a starting point.
A minimal non-bank config needs only `output_schema` + `synonyms` +
`replace_synonyms: true`. See `tests/test_schema.py::test_generic_non_bank_mapper`.

## API reference

Top-level (`from bank_statement_mapper import ...`):

| Symbol | Kind | Notes |
|---|---|---|
| `process_file(path, *, output_format="file", cache=None, table_matcher=None, learn_store=None, threshold=80)` | fn | map a file → `ProcessResult` |
| `process_stream(data, *, output_format="records", cache=None, ...)` | fn | map bytes / a binary stream |
| `MappingCache("<url>")` | class | layout cache; `.get/.put/.close` (sync). No arg → env/sqlite |
| `LearnStore("<url>")` | class | learned synonyms; `.synonyms/.pending/.approve/.reject/.add/.close` |
| `configure(source=None, config=None)` | fn | load output template + synonyms (call once at startup) |
| `apply_learned(store)` | fn | activate a LearnStore's synonyms |
| `learn_from_result(res, store)` / `harvest_folder(dir, store)` | fn | teach the store |
| `load_config` / `config_from_dict` / `Config` | — | build a config object |
| `open_store(url)` | fn | low-level backend factory |
| `ProcessResult`, `ColumnMap`, `OutputResult` | class | result types |
| `records_to_csv_bytes(records)` | fn | CSV serializer |

Submodules: `bank_statement_mapper.ai_matcher` (`OpenAICompatibleMatcher`),
`bank_statement_mapper.bank_mapper_api` (`router`, `lifespan`, `app`, `state`,
`build_matcher`), `bank_statement_mapper.llm_fallback` (`HashingEmbeddingFallback`).

## Gotchas & FAQ

- **"No module named `bank_mapper_cache`" / `MappingCacheManager` not found.**
  Those don't exist. The cache is `from bank_statement_mapper import MappingCache`,
  and it's a plain sync object. The FastAPI `lifespan` already creates it — you
  don't need a manager or a startup hook.
- **The cache is synchronous.** No `await`, no `init_cache()`/`close_cache()`.
  Lifecycle is `MappingCache(...)` and `.close()`.
- **Don't mix `lifespan=` with `@app.on_event(...)`.** Use the `lifespan` (the
  `on_event` API is deprecated in FastAPI, and the lifespan already sets up the cache).
- **Setting an env var after `import` has no effect on config.** Set
  `BANK_MAPPER_CONFIG` before startup, or call `configure(...)` explicitly. The
  router/CLI do this for you.
- **I get `balance` even though my schema drops it.** Your config didn't load —
  the built-in default (which has `balance`) is active. Check the key is exactly
  `output_schema` and that `configure()`/`BANK_MAPPER_CONFIG` actually ran; a bad
  config logs a warning and falls back to defaults.
- **The package created `.db` / `.db-wal` / `.db-shm` files on startup.** Those
  are the SQLite cache + learn store (opened eagerly by the FastAPI `lifespan`);
  the `-wal`/`-shm` are normal WAL sidecars. They're gitignored. Set
  `BANK_MAPPER_CACHE=memory://` and `BANK_MAPPER_LEARN_STORE=memory://` for no
  files, or point them at a path / Redis / Valkey. See [Runtime files](#runtime-files-the-db-db-wal-db-shm-you-may-see).
- **AI never fires.** It's off unless `OPENAI_API_KEY` is set and you pass a
  `table_matcher` (or use the router, which builds one when the key is present).
- **`ModuleNotFoundError: redis`** (or valkey/psycopg). You selected that backend
  but didn't install its extra: `pip install "bank-statement-mapper[redis]"`. The
  default SQLite backend needs nothing.
- **Multiple workers.** SQLite is safe for one host; for several containers point
  `BANK_MAPPER_CACHE`/`BANK_MAPPER_LEARN_STORE` at `redis://` / `valkey://` /
  `postgresql://` so they share state.

## Development

```bash
git clone https://github.com/KarthiKeyan05046/bank-statement-mapper
cd bank-statement-mapper
pip install -e ".[api]" pytest
pytest -q                     # 59 tests
python make_fixtures.py       # regenerate test_statements/
```

## Scope

`.xlsx` only. Library + CLI + FastAPI router. No transaction categorization. The
`02/06` day-vs-month ambiguity resolves per-locale (default day-first) and is
surfaced, never silently guessed.

## License

MIT © Karthikeyan Duraisamy
