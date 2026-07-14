# Tabular Mapper

Map **any spreadsheet (`.xlsx`), in any layout**, to a schema *you* define — the
header row is found automatically, columns are matched to your fields, and
anything ambiguous is flagged for review instead of silently guessed.

The engine is **domain-agnostic** — invoices, product catalogs, payroll, bank
statements. "Bank statements" is just a built-in preset (`bank_preset()`). The
common path is **100% deterministic** (header detection + synonym/fuzzy matching);
an LLM is optional, off by default, and only ever sees column *headers* + column
*structure* — never your cell data.

```python
from tabularmapper import process_file, configure, config_from_dict

configure(config_from_dict({
    "output_schema": [{"field": "sku", "header": "SKU", "type": "text"},
                      {"field": "price", "header": "Unit Price", "type": "number"}],
    "synonyms": {"sku": ["sku", "item code"], "price": ["unit price", "rate"]},
}))
res = process_file("catalog.xlsx")
res.records         # -> [{'sku': 'A-100', 'price': 12.5}, ...]  ready for JSON / a DB
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
pip install tabularmapper                 # core — no DB driver, no AI SDK
pip install "tabularmapper[api]"          # + FastAPI router
pip install "tabularmapper[valkey]"       # + Valkey  (also: redis, postgres, dotenv)
```

The core install pulls only `openpyxl`, `rapidfuzz`, `python-dateutil`. Everything
else (Redis/Valkey/Postgres drivers, FastAPI, dotenv) is an **optional extra** you
add only if you use it. Import name is `tabularmapper`.

## Quickstart

### 1. As a library

```python
from tabularmapper import process_file, process_stream, configure, bank_preset

configure(config=bank_preset())          # or a config_from_dict(...) of your own
res = process_file("file.xlsx")
rows = res.records                       # list[dict], one per row

# from bytes (e.g. an upload) — parsed in memory, nothing written to disk
res = process_stream(open("file.xlsx", "rb").read())

# from a base64 string (e.g. a JSON payload) — decode, then reuse process_stream
from tabularmapper import decode_base64
res = process_stream(decode_base64(payload))   # payload: base64 str/bytes or data: URL
```

Three ways in — `process_file` (path), `process_stream` (bytes / file-like), and
`decode_base64(...) → bytes` fed to `process_stream`. All share one parse path, so
`.xls` vs `.xlsx` is auto-detected regardless of how the bytes arrived.

There is **no default schema** — call `configure(...)` with your own config or a
preset first, otherwise nothing is mapped ([Custom schema](#custom-output-schema)).

### 2. From the command line

```bash
tabularmapper file.xlsx --config schema.json   # your schema
tabularmapper file.xlsx --preset bank          # built-in bank layout
tabularmapper file.xlsx --preset bank --format json    # JSON to stdout
```

### 3. In a FastAPI app

```python
from fastapi import FastAPI
from tabularmapper.api import router, lifespan

app = FastAPI(lifespan=lifespan)              # lifespan wires cache + config + AI for you
app.include_router(router)                    # -> POST /mapper/map, GET /mapper/health, GET /mapper/test
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

For a plain-language walkthrough (mental model, the config file, when the AI runs,
caching, troubleshooting, known limitations), see
**[docs/how-it-works.md](docs/how-it-works.md)**.

## The result object

`process_file` / `process_stream` return a `ProcessResult`:

| Attribute | Type | What it is |
|---|---|---|
| `records` | `list[dict]` | the mapped rows — keys are your schema fields. **Use this for a DB.** |
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
| `TABULARMAPPER_CACHE` | `memory://` (no files) | where header→field mappings are cached ([backends](#storage-backends)) |
| `TABULARMAPPER_LEARN_STORE` | `memory://` (no files) | where self-learned header synonyms live |
| `TABULARMAPPER_CONFIG` | *(none — required)* | output template + synonyms JSON (file / `https://` / `s3://`) |
| `TABULARMAPPER_ROUTE_PREFIX` | `/mapper` | FastAPI router path prefix |
| `TABULARMAPPER_THRESHOLD` | `80` | fuzzy-accept gate (0–100); raise it to push borderline fuzzy matches to the AI matcher |
| `TABULARMAPPER_AI_FILL` | `all` | `all` = AI fills **any** column the rules left unmapped; `critical` = only when a critical field is missing |
| `TABULARMAPPER_AI_SYSTEM_PROMPT` | *(built-in default)* | override the AI matcher's system prompt (or set `ai_system_prompt` in the config) |
| `OPENAI_API_KEY` | *(unset → AI off)* | enables the AI column matcher |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | any OpenAI-compatible endpoint (point at OpenRouter for Anthropic/Gemini/Kimi) |
| `OPENAI_MODEL` | `gpt-4o-mini` | model name |

```bash
export TABULARMAPPER_CACHE="valkeys://default:PASSWORD@host:6379"
```

## Storage backends

The cache and the learn store share one URL convention (like SQLAlchemy/Celery) —
change the backend by changing the URL, nothing else:

| URL | Backend | Install |
|---|---|---|
| `memory://` | in-process, **no files (default)** | — |
| `sqlite:///cache.db` | SQLite file, concurrency-safe, persistent | — |
| `redis://` / `rediss://` | Redis | `pip install "tabularmapper[redis]"` |
| `valkey://` / `valkeys://` | Valkey (Redis fork, e.g. Aiven) | `pip install "tabularmapper[valkey]"` |
| `postgresql://` | Postgres | `pip install "tabularmapper[postgres]"` |

```python
from tabularmapper import MappingCache, process_file

cache = MappingCache("valkeys://default:pw@host:6379")   # or MappingCache() to read the env var
res = process_file("statement.xlsx", cache=cache)
```

`MappingCache` is **synchronous** — `.get()`, `.put()`, `.close()`. There is no
async manager and no `init_cache`/`close_cache`. Selecting the backend is the URL,
full stop.

### Persistence is opt-in (no files by default)

By default the cache and learn store are **in-memory** — the package writes
**no files**. They still cache/learn within a running process (lost on restart).
Turn on persistence only when you want it, by setting a URL:

```bash
# default: nothing set -> in-memory, no files

# persist to a file (creates cache.db + WAL sidecars .db-wal / .db-shm):
TABULARMAPPER_CACHE=sqlite:////var/lib/tabularmapper/cache.db
TABULARMAPPER_LEARN_STORE=sqlite:////var/lib/tabularmapper/learned.db

# or a shared server (survives restarts, shared across workers):
TABULARMAPPER_CACHE=valkeys://user:pw@host:6379
TABULARMAPPER_LEARN_STORE=valkeys://user:pw@host:6379
```

If you *do* use a SQLite URL, the `.db-wal` / `.db-shm` files that appear next to
it are normal Write-Ahead-Logging sidecars (that's what makes it concurrency-safe);
they're checkpointed away on a clean shutdown and are already gitignored.

> In a FastAPI app the `.env` file is **not** auto-loaded (only the CLI does that).
> Call `load_dotenv()` at startup, or run with `uv run --env-file .env`, or the
> env vars won't be seen and you'll get the in-memory default.

## Use with FastAPI

The package ships a ready router. Two ways to use it.

### Simplest — use the built-in lifespan

```python
from fastapi import FastAPI
from tabularmapper.api import router, lifespan

app = FastAPI(lifespan=lifespan)
app.include_router(router)
```

At startup the `lifespan` reads `TABULARMAPPER_CONFIG`, builds `MappingCache()` from
`TABULARMAPPER_CACHE`, builds the learn store, and enables the AI matcher if
`OPENAI_API_KEY` is set. **Configure it entirely with env vars.**

### Control the cache yourself — write your own lifespan

```python
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
import tabularmapper.engine as engine
from tabularmapper.api import router, state, build_matcher
from tabularmapper import MappingCache, LearnStore, apply_learned

@asynccontextmanager
async def lifespan(app: FastAPI):
    engine.configure(os.getenv("TABULARMAPPER_CONFIG"))
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
| `POST` | `/mapper/map` | upload an `.xlsx`, get the mapping + rows (JSON) |
| `GET` | `/mapper/health` | `{status, ai_enabled}` |
| `GET` | `/mapper/test` | test-mapping web page — drop an `.xlsx` and inspect the mapping (schema coverage, per-column reasons, learn queue, download) |
| `GET` | `/mapper/config` | config-builder web page — design a schema, export `config.json` |
| `GET` | `/mapper/config.json` | the mapper's currently-active config as JSON (the page's "Load current" uses this) |
| `GET` | `/mapper/learn/pending` | debit/credit synonyms awaiting approval |
| `POST` | `/mapper/learn/approve` | approve a pending synonym (`?phrase=&field=`) |
| `POST` | `/mapper/learn/reject` | reject a pending synonym |

`POST /mapper/map` reads the upload in memory (no temp file) and runs the
blocking work in a threadpool. Store the original file to S3 in your own endpoint
if you need it — the mapper stays out of AWS.

Two query params shape the request:

```bash
curl -F file=@f.xlsx "http://localhost:8000/mapper/map?format=base64"    # json + a mapped .xlsx in file_base64
curl -F file=@f.xlsx "http://localhost:8000/mapper/map?format=file" -OJ  # download the mapped .xlsx
curl -F file=@f.xlsx "http://localhost:8000/mapper/map?threshold=90"     # stricter fuzzy gate for this call
```

`format` is `json` (default) / `base64` / `file`. `threshold` (0–100) overrides
`TABULARMAPPER_THRESHOLD` for one request — raise it to send borderline fuzzy
matches to the AI matcher instead of trusting them.

The `/mapper` prefix is configurable (this is a general table→schema mapper, not
just banks): set `TABULARMAPPER_ROUTE_PREFIX`, or build the router yourself:

```python
from tabularmapper.api import make_router, lifespan
app.include_router(make_router("/catalog"))     # -> POST /catalog/map, ...
```

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

CSV: `from tabularmapper import records_to_csv_bytes`.

## AI column matcher (optional)

For a brand-new bank whose headers the synonyms can't place, one LLM call maps the
whole header row. It's **off unless `OPENAI_API_KEY` is set**, and the prompt
contains only column headers + structural metadata (types, fill rate, which
columns are mutually exclusive) — **never a transaction value**.

```python
from tabularmapper.ai_matcher import OpenAICompatibleMatcher
res = process_file("new_bank.xlsx", table_matcher=OpenAICompatibleMatcher())
```

Works with OpenAI, Azure, Together, Groq, or a local vLLM/Ollama endpoint via
`OPENAI_BASE_URL`.

### Any provider (Anthropic, Gemini, Kimi, …) via OpenRouter

The matcher speaks the OpenAI `/chat/completions` API, and [OpenRouter](https://openrouter.ai)
exposes **every** major model through exactly that interface — so you can use
Anthropic, Gemini, Kimi K2, DeepSeek, etc. with **no extra dependency** and no code
change. Just point the three env vars at OpenRouter and pick a model:

```bash
OPENAI_BASE_URL=https://openrouter.ai/api/v1
OPENAI_API_KEY=sk-or-...
OPENAI_MODEL=google/gemini-2.5-flash      # or anthropic/claude-3.5-haiku, moonshotai/kimi-k2, openai/gpt-4o-mini
```

Model matters for this task: a small local 7B model is unreliable at column mapping —
prefer a `gpt-4o-mini`/`gemini-flash`/`haiku`-class model (or Kimi K2). Because the
AI only runs on unknown layouts and the result is cached, cost is negligible, so
optimize for reliability, not price.

### Customizing the AI system prompt

The matcher's system prompt is domain-neutral by default. To tune it for your domain,
set it three ways (highest priority first): the `system_prompt=` arg to
`OpenAICompatibleMatcher`, an `ai_system_prompt` field in your config JSON, or the
`TABULARMAPPER_AI_SYSTEM_PROMPT` env var. The JSON-output contract lives in the user
message (always sent), so overriding the system prompt is safe.

```json
{ "output_schema": [...], "synonyms": {...},
  "ai_system_prompt": "You map e-commerce product export columns to a fixed schema. ..." }
```

## Self-learning

When the AI resolves a new header, it's remembered so the next statement from that
bank maps deterministically (an `exact` match) with no AI call. Fields listed in the
config's **`gated_fields`** are held for a one-time human approval (via
`/learn/pending` + `/learn/approve`); everything else auto-applies. Nothing is gated
by default; `bank_preset()` gates `debit`/`credit` (a wrong direction is the costly
error). Use a persistent `TABULARMAPPER_LEARN_STORE` (sqlite/redis/postgres) so it
converges across uploads instead of forgetting on restart.

```python
from tabularmapper import LearnStore, apply_learned, process_file
from tabularmapper.ai_matcher import OpenAICompatibleMatcher

store = LearnStore()                         # TABULARMAPPER_LEARN_STORE or sqlite
apply_learned(store)                         # activate at startup
res = process_file("stmt.xlsx", table_matcher=OpenAICompatibleMatcher(), learn_store=store)

store.pending()                              # debit/credit awaiting review
store.approve("outgoing", "debit")           # now an exact match everywhere
```

Bootstrap from an archive in one pass:
`tabularmapper --harvest ./past_statements --learn sqlite:///learned.db`.

## Custom output schema

The output columns and synonyms are data, not code. Point `TABULARMAPPER_CONFIG` at
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

`type` is `date` | `number`/`money`/`currency`/`integer`/`float` | `text`/`string`.
Rename a header, reorder, drop a column, or add a brand-new one — all config, no
code. In a library call `configure("config.json")` (or `configure(config_from_dict(...))`)
before processing. **There is no default schema** — `synonyms` are exactly what
you declare (nothing is merged in).

Optional keys, all data-driven (omit them for a plain type-based mapping):

| Key | What it does |
|---|---|
| `output_schema[].description` | hint for the AI matcher (falls back to the field name) |
| `critical_fields` | fields that must be mapped, else `needs_review` |
| `require_any` | `[[a, b]]` — each group needs ≥1 mapped field, else `needs_review` |
| `reconcile` | Split one amount column into two directional fields. **Sign mode:** `{"signed":s,"negative":n,"positive":p}` (e.g. `-500`→debit, `+500`→credit). **Direction mode:** add `"direction":flag` (+ optional `negative_values`/`positive_values`) to route an *unsigned* amount by a separate flag column (`Type`=DEBIT/CREDIT, `DR/CR`). Also handles plain separate debit/credit columns. |
| `gated_fields` | fields whose AI/harvest-learned synonyms wait in `/learn/pending` instead of auto-applying (e.g. `["debit","credit"]`); empty by default |
| `ai_system_prompt` | override the AI matcher's system prompt for this schema |
| `row_keep_if_any` | a row is a record only if ≥1 of these has a value (default: any non-empty) |
| `continuation_field` | a row with only this field folds into the row above (multi-line cells) |

The ready-made **bank preset** is in `config.example.json` (also `bank_preset()`
in code) — copy it as a starting point. A minimal config needs only
`output_schema` + `synonyms`. See `tests/test_schema.py::test_generic_custom_config`.

## API reference

Top-level (`from tabularmapper import ...`):

| Symbol | Kind | Notes |
|---|---|---|
| `process_file(path, *, output_format="file", cache=None, table_matcher=None, learn_store=None, threshold=80)` | fn | map a file → `ProcessResult` |
| `process_stream(data, *, output_format="records", cache=None, ...)` | fn | map bytes / a binary stream |
| `decode_base64(data) → bytes` | fn | decode a base64 str/bytes (or `data:` URL) → raw bytes for `process_stream`; `ValueError` if not base64 |
| `MappingCache("<url>")` | class | layout cache; `.get/.put/.close` (sync). No arg → env/sqlite |
| `LearnStore("<url>")` | class | learned synonyms; `.synonyms/.pending/.approve/.reject/.add/.close` |
| `configure(source=None, config=None)` | fn | load output template + synonyms (call once at startup) |
| `apply_learned(store)` | fn | activate a LearnStore's synonyms |
| `learn_from_result(res, store)` / `harvest_folder(dir, store)` | fn | teach the store |
| `load_config` / `config_from_dict` / `Config` | — | build a config object |
| `open_store(url)` | fn | low-level backend factory |
| `ProcessResult`, `ColumnMap`, `OutputResult` | class | result types |
| `records_to_csv_bytes(records)` | fn | CSV serializer |

Submodules: `tabularmapper.ai_matcher` (`OpenAICompatibleMatcher`),
`tabularmapper.api` (`router`, `lifespan`, `app`, `state`,
`build_matcher`), `tabularmapper.llm_fallback` (`HashingEmbeddingFallback`).

## Gotchas & FAQ

- **"No module named `bank_mapper_cache`" / `MappingCacheManager` not found.**
  Those don't exist. The cache is `from tabularmapper import MappingCache`,
  and it's a plain sync object. The FastAPI `lifespan` already creates it — you
  don't need a manager or a startup hook.
- **The cache is synchronous.** No `await`, no `init_cache()`/`close_cache()`.
  Lifecycle is `MappingCache(...)` and `.close()`.
- **Don't mix `lifespan=` with `@app.on_event(...)`.** Use the `lifespan` (the
  `on_event` API is deprecated in FastAPI, and the lifespan already sets up the cache).
- **Setting an env var after `import` has no effect on config.** Set
  `TABULARMAPPER_CONFIG` before startup, or call `configure(...)` explicitly. The
  router/CLI do this for you.
- **I get `balance` even though my schema drops it.** Your config didn't load —
  the built-in default (which has `balance`) is active. Check the key is exactly
  `output_schema` and that `configure()`/`TABULARMAPPER_CONFIG` actually ran; a bad
  config logs a warning and falls back to defaults.
- **`.db` files appear even though I set `memory://` in `.env`.** In a FastAPI
  app the `.env` isn't auto-loaded, so your env vars aren't seen and it uses the
  default. The default is now **in-memory (no files)** — but if you're on an
  older build it defaulted to SQLite. Either upgrade, `load_dotenv()` at startup,
  or run with `uv run --env-file .env`. See [Persistence is opt-in](#persistence-is-opt-in-no-files-by-default).
- **AI never fires.** It's off unless `OPENAI_API_KEY` is set and you pass a
  `table_matcher` (or use the router, which builds one when the key is present).
- **`ModuleNotFoundError: redis`** (or valkey/psycopg). You selected that backend
  but didn't install its extra: `pip install "tabularmapper[redis]"`. The
  default SQLite backend needs nothing.
- **Multiple workers.** SQLite is safe for one host; for several containers point
  `TABULARMAPPER_CACHE`/`TABULARMAPPER_LEARN_STORE` at `redis://` / `valkey://` /
  `postgresql://` so they share state.

## Development

```bash
git clone https://github.com/KarthiKeyan05046/tabularmapper
cd tabularmapper
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
