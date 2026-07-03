# tabularmapper — How It Works (plain-language guide)

Written for someone who is *using* and shaping this tool, not necessarily a Python
developer. No prior Python knowledge assumed. If a term looks unfamiliar, check the
**Glossary** at the bottom.

---

## 1. What this tool is, in one breath

You have messy spreadsheets — bank statements, vendor exports, anything — where the
columns are named differently every time and there's junk at the top. This tool
reads any such `.xlsx` and turns it into **clean, uniform rows that always have the
same field names**, so your database or code can trust the shape.

The key idea: **you describe the columns you want once** (a "schema"), and the tool
figures out how each messy file maps onto it — even when the headers are worded
differently. A new layout doesn't need new code.

---

## 2. The mental model (the whole pipeline in four steps)

Every file goes through the same four steps:

```
  messy .xlsx
      │
      ▼
  1. FIND the header row     → skip logos/titles/blank rows, locate the real column titles
      ▼
  2. MAP the columns         → "Withdrawal" → debit, "Narration" → description, ...
      ▼
  3. PARSE the values        → "1,200.50" → 1200.5, "01-06-2026" → 2026-06-01, "500 Dr" → -500
      ▼
  4. REVIEW gate             → if anything is uncertain, flag needs_review instead of guessing
      ▼
  clean rows (same fields every time)
```

Two promises baked into this design:

- **A model never sees your transaction data.** Steps 1–3 are pure rules (math and
  string matching). The optional AI (see §7) only ever looks at *column structure*,
  never the numbers, names, or narrations.
- **It won't silently guess.** If it isn't confident, it sets `needs_review` and tells
  you why, rather than shipping a wrong number.

---

## 3. The one thing you actually edit: the config

Almost everything you'll tune lives in **one JSON file** (usually `config.json`). This
is the tool's brain. Out of the box the tool ships **empty** — it maps nothing until
you give it a config. (There's a built-in bank layout you can load instead; see §9.)

A config has two required parts and a few optional knobs.

### Required

**`output_schema`** — the list of fields you want out, and their types:

```json
"output_schema": [
  { "field": "date",        "header": "Date",      "type": "date" },
  { "field": "description", "header": "Narration",  "type": "text" },
  { "field": "debit",       "header": "Debit",      "type": "money" },
  { "field": "credit",      "header": "Credit",     "type": "money" }
]
```

- `field` — the internal name you'll see in the output rows (`row["debit"]`).
- `header` — a display label (used when writing an output spreadsheet).
- `type` — how to parse the values. See the type list below.
- `description` (optional) — a plain-English hint that helps the AI understand the
  field. Worth filling in if you use AI.

**`synonyms`** — the alternative header wordings that should map to each field. This
is what makes a new layout "just work":

```json
"synonyms": {
  "date":  ["date", "txn date", "value date", "posting date"],
  "debit": ["debit", "withdrawal", "paid out", "dr", "amount withdrawn"],
  "credit":["credit", "deposit", "paid in", "cr", "amount deposited"]
}
```

> **Rule of thumb:** every time a file fails to map a column, add that column's
> wording to the right synonym list. That one-line edit turns a shaky guess into a
> rock-solid exact match. This is the single most useful thing you'll do.

### Types you can use in `type`

| You write | Meaning |
|---|---|
| `date`, `datetime` | parsed to `YYYY-MM-DD` (never day/month-flipped) |
| `money`, `number`, `currency`, `decimal`, `float`, `numeric` | parsed to a number; handles commas, `₹`/`$`, `(500)`, `500 Dr` |
| `integer`, `int` | number with no decimals |
| `text`, `string`, `str` | kept as text |

### Optional knobs (only if you need them)

| Key | What it does |
|---|---|
| `critical_fields` | Fields that MUST be present, or the whole file is flagged for review. |
| `require_any` | "At least one of these must map." E.g. `[["debit","credit","amount"]]` means the file needs *some* money column. |
| `reconcile` | Split ONE signed column into two. `{"signed":"amount","negative":"debit","positive":"credit"}` turns `-500` into `debit=500` and `+500` into `credit=500`. |
| `row_keep_if_any` | Keep a row only if one of these fields has a value (drops footer/blank rows). |
| `continuation_field` | If a row has only this field filled (e.g. a wrapped description line), merge it into the previous row. |

You don't have to understand all of these on day one. Start with `output_schema` +
`synonyms`; add the rest when a real file needs them.

---

## 4. Three ways to run it

### A) The quick one-liner (a tiny script)

```python
from tabularmapper import process_file, configure

configure("config.json")            # load your brain file
result = process_file("statement.xlsx")

print(result.records)               # the clean rows, as a list of dictionaries
print(result.needs_review)          # True/False
```

### B) The command line (no code at all)

```bash
tabularmapper statement.xlsx --config config.json --format json
```

Handy flags: `--format json|file|base64|records`, `--ai` (turn AI on),
`--cache <url>`, `--preset bank`, `--harvest <folder>` (learn from a pile of files),
`--threshold 80`.

### C) Inside your web backend (FastAPI)

```python
from dotenv import load_dotenv
load_dotenv()                       # FastAPI does NOT auto-read .env — do it yourself

from fastapi import FastAPI
from tabularmapper.api import router, lifespan
from tabularmapper import configure

configure("config.json")
app = FastAPI(lifespan=lifespan)
app.include_router(router)
```

This gives you ready-made endpoints (default prefix `/mapper`):

- `POST /mapper/map` — upload an `.xlsx`, get the clean rows back.
- `GET  /mapper/health` — is it up, is AI enabled?
- `GET/POST /mapper/learn/*` — the self-learning queue (see §10).

Upload and choose your output shape with `?format=`:

```bash
curl -F file=@statement.xlsx "http://localhost:8000/mapper/map"                # JSON rows
curl -F file=@statement.xlsx "http://localhost:8000/mapper/map?format=base64"  # rows + an .xlsx in file_base64
curl -F file=@statement.xlsx "http://localhost:8000/mapper/map?format=file" -OJ # download the .xlsx
```

---

## 5. How a column gets matched (and how to read the result)

For each column, the tool tries, in order:

1. **Exact** — the header matches a synonym exactly → `confidence 100`.
2. **Fuzzy** — close-enough string similarity clears a bar (default 80) → `confidence = score`.
3. **AI** — only if a *critical* field is still missing and AI is enabled (see §7).

Every column in the output carries a `method` and a `confidence` so you can see *how*
it was decided:

| `method` | confidence | meaning |
|---|---|---|
| `exact` | 100 | matched a synonym exactly (the goal) |
| `fuzzy` | 80–99 | similar enough to accept |
| `ai` | 85 | the AI resolved it |
| `field: null` | low | nothing was confident enough → left unmapped → review |

> **To find AI-mapped columns:** look for `method == "ai"`.
> **To fix a shaky `fuzzy` or a `null`:** add that header wording to `synonyms` — it
> becomes `exact` next time.
> **To distrust weak fuzzy matches and send them to AI instead:** raise the
> threshold above their score — `TABULARMAPPER_THRESHOLD` (server-wide) or
> `?threshold=` on one `/map` call. A fuzzy match only becomes a *gap* (and only
> then reaches AI) when its score falls below the gate. Note the gate is global,
> not per-column.

---

## 6. What comes out

`process_file(...)` gives you a `result` with:

- `result.records` — the clean rows (list of dictionaries, ready for JSON or a database).
- `result.needs_review` — `True` if something was uncertain.
- `result.review_reasons` — plain-text explanations when review is needed.
- `result.column_maps` — the per-column decisions (raw header → field, method, confidence).
- `result.header_index` — which row the real headers were on.

The API's `POST /map` returns the same information as JSON, and can also hand back a
cleaned `.xlsx` (as a download or base64) — see §4C.

---

## 7. When the AI runs (and why it's safe)

AI is a **last resort**, not the default. It fires **only** when all of these are true:

1. AI is enabled (`OPENAI_API_KEY` is set, or you passed a matcher).
2. The rules (exact + fuzzy) left something unmapped. By default
   (`TABULARMAPPER_AI_FILL=all`) that means **any** unmapped column, so the AI also
   fills non-critical leftovers like a reference column. Set
   `TABULARMAPPER_AI_FILL=critical` to fire only when a **critical** field is missing.
3. This exact header layout isn't already in the cache.

So if your synonyms already cover a file's columns, **AI never runs** — it's free and
fully deterministic. AI is reserved for genuinely new layouts.

> **By default AI fills any unmapped column** (`TABULARMAPPER_AI_FILL=all`), so a
> non-critical column like reference *does* get an AI attempt. If you set
> `TABULARMAPPER_AI_FILL=critical`, AI only fires on a missing **critical** field —
> then a non-critical `field: null` was never sent to the model, and you'd fix it with
> a synonym or by marking the field critical. Either way, AI still only runs on an
> **uncached** layout (see §8). (See §12.)

**Privacy:** when it does run, the tool computes a *structural profile* of each column
locally (data type, how often it's filled, whether values go negative, which columns
are never filled together) and sends **only that plus the header words** to the model.
No transaction amount, date, name, or narration ever leaves the machine.

Turn it on for the API by setting environment variables — no code change:

```bash
export OPENAI_API_KEY=sk-...
export OPENAI_BASE_URL=https://api.openai.com/v1   # optional; works with Groq/Together/Ollama/etc.
export OPENAI_MODEL=gpt-4o-mini                     # optional
```

`GET /mapper/health` will then report `"ai_enabled": true`.

**Want a provider that isn't OpenAI (Anthropic, Gemini, Kimi)?** Don't add an SDK —
point the same three env vars at [OpenRouter](https://openrouter.ai), which serves
every model through the OpenAI API the matcher already speaks:

```bash
OPENAI_BASE_URL=https://openrouter.ai/api/v1
OPENAI_API_KEY=sk-or-...
OPENAI_MODEL=google/gemini-2.5-flash   # or anthropic/claude-3.5-haiku, moonshotai/kimi-k2, ...
```

Use a capable model (a `gpt-4o-mini`/`gemini-flash`/`haiku`-class model or Kimi K2) —
small 7B models are unreliable at column mapping.

**Tuning the prompt.** The matcher ships a domain-neutral system prompt with
conservative, bounded rules (semantics over string-matching; unresolved columns →
`null`; don't over-map). To adapt it to your domain, set — highest priority first —
`OpenAICompatibleMatcher(system_prompt=...)`, an `ai_system_prompt` field in your
config JSON, or the `TABULARMAPPER_AI_SYSTEM_PROMPT` env var. The JSON-output contract
lives in the user message, so overriding the system prompt is always safe.

---

## 8. The cache (so it's fast and cheap)

Once a header layout is successfully mapped, the tool **remembers the whole mapping**
keyed to that layout. The next file with the same headers skips all the work (and never
touches the AI). This is automatic.

- **When it saves:** only when the result is clean (`needs_review == False`). It refuses
  to cache a shaky guess, so a bad mapping can never get "promoted" to trusted.
- **A subtle trap with unmapped columns:** a null **critical** field trips
  `needs_review`, so it's never cached — good. But a null **non-critical** field does
  *not* trip review, so the whole mapping (including that null) *can* get cached. Next
  time it's replayed as null, and because the field isn't critical, AI won't re-fire for
  it either — the column is stuck null. Fix: if you care about that field, mark it
  `critical` or give it a synonym; don't leave it non-critical and hope AI catches it.
- **Where it saves:** controlled by one environment variable, `TABULARMAPPER_CACHE`.
  Default is `memory://` (in-memory, nothing written to disk). Point it at a database to
  make it durable and shared:

```
TABULARMAPPER_CACHE=memory://              # default — nothing persists
TABULARMAPPER_CACHE=sqlite:///cache.db     # a local file
TABULARMAPPER_CACHE=redis://host:6379/0    # shared, fast
TABULARMAPPER_CACHE=valkey://host:6379/0   # Redis-compatible (e.g. Aiven)
TABULARMAPPER_CACHE=postgresql://user:pw@host/db
```

> **Gotcha (you hit this):** in a FastAPI app, `.env` is **not** auto-loaded. If you set
> `TABULARMAPPER_CACHE` in `.env` but never call `load_dotenv()`, the tool silently uses
> the in-memory default. And it must be spelled `TABULARMAPPER_CACHE` exactly — the old
> `BANK_MAPPER_CACHE` name does nothing.

---

## 9. The built-in bank preset

You don't have to write a bank config from scratch — one ships with the tool:

```python
from tabularmapper import configure, bank_preset
configure(config=bank_preset())     # date, description, reference, debit, credit, balance, amount
```

or on the command line: `tabularmapper statement.xlsx --preset bank`. Use it as-is, or
copy `config.example.json` (which is this same layout) and edit it into your own.

---

## 10. Self-learning vocabulary (optional)

When AI or a human confirms a new header wording, the tool can **remember it as a
synonym** so it becomes an instant exact match next time — the tool gets smarter and
cheaper the more you use it. Sensitive fields (like debit/credit) are held in a
**pending review queue** instead of auto-trusted, which you approve via the
`/mapper/learn/*` endpoints. This is off unless you wire up a learn store
(`TABULARMAPPER_LEARN_STORE`, same URL styles as the cache).

---

## 11. Environment variables — the full list

| Variable | Purpose | Default |
|---|---|---|
| `TABULARMAPPER_CONFIG` | Path/URL to your config JSON | none (you must configure) |
| `TABULARMAPPER_CACHE` | Where the mapping cache lives | `memory://` |
| `TABULARMAPPER_LEARN_STORE` | Where learned synonyms live | `memory://` |
| `TABULARMAPPER_ROUTE_PREFIX` | API route prefix | `/mapper` |
| `TABULARMAPPER_THRESHOLD` | Fuzzy-accept gate (0–100); raise it to route borderline matches to AI | `80` |
| `TABULARMAPPER_AI_FILL` | `all` = AI fills any unmapped column; `critical` = only on a missing critical field | `all` |
| `TABULARMAPPER_AI_SYSTEM_PROMPT` | Override the AI matcher's system prompt (or set `ai_system_prompt` in the config) | built-in default |
| `OPENAI_API_KEY` | Enables the AI matcher | unset (AI off) |
| `OPENAI_BASE_URL` | AI endpoint (point at OpenRouter for Anthropic/Gemini/Kimi) | OpenAI's |
| `OPENAI_MODEL` | AI model name | `gpt-4o-mini` |

Remember: in a plain script or the CLI these can come from `.env`, but **in FastAPI you
must `load_dotenv()` yourself** or export them in the environment.

---

## 12. Troubleshooting (the things that actually bite)

| Symptom | Cause | Fix |
|---|---|---|
| Output has default/empty fields | No config loaded (default is empty) | `configure("config.json")` or `--preset bank` |
| A column is `field: null` — **even with AI on** | AI didn't rescue it: the AI call failed, the column genuinely matches no schema field, the model gave up (weak model / no `description`), the layout was cached before AI ran, or you set `TABULARMAPPER_AI_FILL=critical` and the field isn't critical | Confirm `ai_enabled: true` and use a capable model + a good `description`; or add the header wording to `synonyms` (reliable, free). If the column isn't a field you want, null is correct. |
| "But AI should map it!" | By default (`AI_FILL=all`) it does try — unless AI is off, the call failed, the model missed it, or the layout is cached | Check `GET /mapper/health` (`ai_enabled`), use a capable model, add a `description`; a synonym is the guaranteed fix |
| A `null` column keeps coming back, AI never re-fires | It was cached (non-critical nulls get cached; the cache hit then skips AI) | Add a synonym, or clear the cache to force a re-map |
| Cache "isn't working" | Wrong env name, or `.env` not loaded in FastAPI | Use `TABULARMAPPER_CACHE` (not `BANK_MAPPER_*`) and call `load_dotenv()` |
| AI never runs | No `OPENAI_API_KEY`, the rules mapped every column, or it's a cached layout (or `AI_FILL=critical` with all criticals mapped) | Set the key; AI fires on any unmapped column (default) in an **uncached** layout |
| `needs_review` is `True` | A critical field is missing or a column is low-confidence | Read `review_reasons`; usually a missing synonym |
| Changed the config, old mapping still used | The cache is scoped to the schema, but a stale process may hold the old one | Restart the app / clear the cache |

---

## 13. Known limitations

- **One active config per process.** `configure()` sets a single global config, so
  one running app maps one schema. To serve two domains (e.g. bank statements *and*
  employee records) today you'd run two app instances with different configs.
  Multi-schema-in-one-app (select a named config per request, e.g. `?schema=bank`)
  is planned — it's a real refactor, not a flag, because the engine currently reads
  one global config. Until then, one schema per deployment.
- **AI accuracy tracks the model.** A small local ≤8B model is unreliable at column
  mapping; use a `gpt-4o-mini`/`gemini-flash`/`haiku`-class model or Kimi K2. Rich
  synonyms keep AI off the critical path so the model rarely matters.
- **Tabular `.xlsx` only.** This maps spreadsheet columns; it is not OCR for scanned
  PDFs or a parser for free-text statements.

---

## Glossary

- **Schema** — the fixed list of output fields you want (your "target shape").
- **Header row** — the row in the spreadsheet that holds the column titles.
- **Synonym** — an alternative wording for a field (e.g. "Withdrawal" for `debit`).
- **Fuzzy match** — accepting a header because it's *similar enough*, not identical.
- **Confidence** — 0–100 score for how sure a column mapping is.
- **`needs_review`** — the tool's "I'm not sure, a human should look" flag.
- **Cache** — remembered mappings so repeat layouts are instant and free.
- **Deterministic** — same input always gives the same output (rules, not guessing).
- **Config** — the one JSON file that defines your schema, synonyms, and rules.
- **Environment variable** — a setting read from outside the code (the shell or `.env`).

---

*This document describes tabularmapper. The behavior above is what the code does today;
if you change the engine, update this file so the next person can trust it.*
