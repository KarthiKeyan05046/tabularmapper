# Self-Learning Column Mapping — Design, Plan & README

Goal: stop hand-editing the `SYNONYMS` dict in `bank_mapper.py`. Make the
header→field vocabulary **external, data-driven, and self-growing**, so a new
bank format teaches the system once and never needs a code change.

---

## STATUS: implemented (in `learn.py`)

The core loop is built. What actually shipped differs from the original plan
below in two ways, both improvements:

- **Storage uses the `open_store(url)` convention** (see `stores.py`), not a
  bespoke `synonyms.json`. Learned synonyms live in the same SQLite/Redis/Postgres
  backend as the cache — concurrency-safe, selected by `BANK_MAPPER_LEARN_STORE`.
- **Config ↔ learned split is enforced:** the config (`config.json`,
  output_schema + seed synonyms) is read-only from S3/URL; the *learned* half is
  the writeable store. Effective vocabulary at match time = seed + learned, with
  the seed authoritative on conflict.

Shipped API:

```python
from learn import LearnStore, learn_from_result
from bank_mapper import apply_learned, process_file

store = LearnStore()                    # BANK_MAPPER_LEARN_STORE or sqlite default
apply_learned(store)                    # activate learned synonyms (call at startup)

# per statement: process + learn in one call
res = process_file("new_bank.xlsx", table_matcher=matcher, learn_store=store)

# trust policy: date/description/reference/balance auto-apply; debit/credit are
# GATED to a review queue (a wrong direction is the costly error):
store.pending()                         # [{phrase, field, source, ...}]
store.approve("outgoing", "debit")      # -> now an exact match everywhere
store.reject("incoming", "credit")
store.stats()                           # {applied, pending, conflicts}
```

FastAPI: the router auto-loads the store at startup, learns on every `/map`, and
exposes `GET /statements/learn/pending`, `POST /statements/learn/approve`,
`POST /statements/learn/reject`. Set `auto_apply_gated=True` on `LearnStore` for
a fully unattended loop (no review queue).

Not yet built from the plan below: the `harvest_folder` bootstrap (§7).

---

## 1. Why change

Today `SYNONYMS` is a Python constant. Adding a bank means editing code and
redeploying. That doesn't scale and blocks non-developers. We already have two
assets that make manual editing unnecessary:

- the **AI table matcher**, which correctly maps unknown headers, and
- the **mapping cache**, which already records confirmed `header → field` decisions.

We just aren't feeding what they learn back into the vocabulary.

## 2. The four options (and the recommendation)

| # | Approach | Effort | Removes manual work? | Notes |
|---|----------|--------|----------------------|-------|
| 1 | Externalize `SYNONYMS` to `synonyms.json` | Low | Partly (edit config, not code) | Foundation for the rest |
| 2 | **Learn from AI/human confirmations** (write back) | Medium | **Yes** — grows itself | The real fix |
| 3 | Harvest from a folder of past statements | Medium | Bootstraps + tops up | Uses data you already have |
| 4 | Drop synonyms, use AI for everything | Low | Yes, but | Loses free/deterministic/auditable path; LLM cost per file |

**Recommended: 1 + 2 + 3.** Keep the fast deterministic layer, but let it learn.
Option 4 is rejected — the whole value of this project is that the common path is
deterministic and only the genuinely new goes to the model.

## 3. Target architecture

```
                       ┌──────────────────────────┐
   statement.xlsx ───▶ │ detect header (Stage 1)  │
                       └──────────┬───────────────┘
                                  ▼
                       ┌──────────────────────────┐   exact/fuzzy hit
                       │ map_columns (Stage 2)    │───────────────▶ done (free)
                       │  uses SYNONYMS loaded     │
                       │  from synonyms.json       │
                       └──────────┬───────────────┘
                                  │ unknown header
                                  ▼
                       ┌──────────────────────────┐
                       │ AI table matcher         │
                       └──────────┬───────────────┘
                                  │ mapping resolved + trusted
                                  ▼
                       ┌──────────────────────────┐
                       │  LEARN: append raw header │  ← the feedback loop
                       │  phrase to synonyms.json  │
                       └──────────────────────────┘
                                  │
                                  ▼
                    next time that header is an EXACT match — no AI
```

Three additions to the codebase:

1. **`synonyms.json`** — the vocabulary, on disk.
2. **`synonyms.py`** — load / save / learn / harvest logic.
3. Small hooks in `bank_mapper.py` (load at import) and `process_file`
   (call `learn_from_result` after a confident map).

## 4. `synonyms.json` format

Two supported shapes. Start simple; upgrade to the rich form when you want
provenance/governance.

Simple (drop-in replacement for the current dict):

```json
{
  "date": ["date", "txn date", "value date", "posting date"],
  "description": ["narration", "particulars", "details"],
  "debit": ["debit", "withdrawal", "paid out"],
  "credit": ["credit", "deposit", "paid in"],
  "reference": ["reference", "cheque no", "utr"],
  "balance": ["balance", "closing balance"],
  "amount": ["amount", "signed amount"]
}
```

Rich (recommended — tracks where each phrase came from, so you can audit/roll back):

```json
{
  "version": 2,
  "fields": {
    "debit": [
      { "phrase": "withdrawal", "source": "seed" },
      { "phrase": "paid out",   "source": "seed" },
      { "phrase": "outgoing",   "source": "ai",    "added": "2026-07-01", "bank": "ACME" },
      { "phrase": "amount dr",  "source": "human",  "added": "2026-07-02" }
    ]
  }
}
```

`source` ∈ `seed | ai | human | harvest`. Everything the code appends is tagged,
so you can review or purge auto-learned entries without touching the seeds.

## 5. Public API (new `synonyms.py`)

```python
load_synonyms(path="synonyms.json") -> dict[str, list[str]]
    # returns the simple field->phrases map that map_columns already expects.
    # falls back to a baked-in DEFAULT_SYNONYMS if the file is missing.

save_synonyms(data, path="synonyms.json") -> None

learn_mapping(header: str, field: str, *, source="ai", bank=None,
              path="synonyms.json") -> bool
    # normalize + dedupe; append `header` under `field` if new.
    # returns True if something was added. Detects conflicts (same phrase
    # already mapped to a DIFFERENT field) and refuses silently -> logs for review.

learn_from_result(result: ProcessResult, *, min_confidence=85,
                  methods=("ai", "cache"), path="synonyms.json") -> list[str]
    # walk result.column_maps; for each confident, non-exact mapping, call
    # learn_mapping. Returns the phrases learned. Call this after process_file.

harvest_folder(folder: str, *, table_matcher=None,
               min_confidence=85, path="synonyms.json") -> dict
    # run the mapper over every .xlsx in `folder`, collect confident
    # header->field pairs, merge into synonyms.json. Bootstrap from your archive.
```

## 6. The learning loop (how manual work disappears)

1. A new bank arrives. Its header (`"Withdrawals"`) isn't in `synonyms.json`, so
   exact/fuzzy miss and the **AI matcher** maps it → `debit` (confidence 85).
2. `process_file` finishes; the caller (or the router) calls
   `learn_from_result(res)`.
3. `learn_mapping("Withdrawals", "debit", source="ai", bank="ACME")` appends
   `"withdrawals"` under `debit` in `synonyms.json`.
4. **Every future statement** with a "Withdrawals" column is now an **exact match**
   — free, instant, deterministic, no AI call.

The synonym list becomes a cache of *vocabulary* (works across banks), while
`mapping_cache.json` stays a cache of *exact layouts* (per-bank). Together they
drive the AI call rate toward zero over time.

### Trust policy (important for financial data)

- `source="ai"` entries can be **auto-applied** (fast) or held in a
  `pending_review` bucket until a human approves (safe). Pick per your risk
  appetite via a `require_review` flag.
- `learn_mapping` refuses to overwrite a phrase already mapped to a different
  field, and records the conflict for a human. No silent debit/credit flips.

## 7. Harvesting from your existing xlsx (bootstrap)

You already have a folder of past statements. Seed the vocabulary in one shot:

```python
from synonyms import harvest_folder
from ai_matcher import OpenAICompatibleMatcher

report = harvest_folder("samples/", table_matcher=OpenAICompatibleMatcher())
# -> {"added": 14, "conflicts": 1, "files": 23, "unresolved": 2}
```

For each file it detects the header, maps columns (deterministic + AI), and
records every confident `header → field` pair. This is the "extract from input
xlsx" you asked about — the headers become synonyms automatically.

## 8. Wiring into `bank_mapper.py` (minimal change)

```python
# top of bank_mapper.py
from synonyms import load_synonyms
SYNONYMS = load_synonyms()          # was a hardcoded dict; now loaded from JSON
# _EXACT_LOOKUP rebuilt from SYNONYMS exactly as today
```

```python
# in the API router / your endpoint, after a successful map:
from synonyms import learn_from_result
res = process_stream(data, table_matcher=MATCHER, cache=CACHE)
learn_from_result(res)              # grow the vocabulary from what the AI resolved
```

Nothing else in `map_columns` changes — it still reads `SYNONYMS`.

## 9. Phased implementation plan

- **Phase 1 — Externalize (½ day).** Add `synonyms.py` with
  `load_synonyms`/`save_synonyms` + `DEFAULT_SYNONYMS` (the current dict). Ship a
  generated `synonyms.json`. `bank_mapper` loads from it. Tests: identical
  mapping behavior to today. *Outcome: edit vocabulary without code changes.*
- **Phase 2 — Learn (1 day).** Add `learn_mapping` + `learn_from_result` with
  normalization, dedupe, and conflict detection. Call it from the router. Tests:
  a new header maps via AI once, then via `exact` on the second run with the AI
  disabled. *Outcome: self-growing vocabulary.*
- **Phase 3 — Harvest (½ day).** Add `harvest_folder` + a CLI subcommand
  (`python -m synonyms harvest ./archive`). *Outcome: bootstrap from history.*
- **Phase 4 — Governance (optional).** Rich JSON with `source`, a `pending_review`
  bucket, a `--review` CLI to approve/reject auto-learned entries, conflict report.

## 10. Risks & decisions to make

- **Auto-trust vs human-approve for `ai` entries.** Auto = zero manual work but a
  wrong AI guess enters the vocabulary; approve = safe but a person confirms new
  phrases. Recommendation: auto-apply but keep `source` tags so you can audit and
  bulk-revert. High-stakes columns (debit/credit) can require review while
  date/description auto-apply.
- **Concurrency.** Like `mapping_cache.json`, `synonyms.json` is a file — guard
  writes (lock or single writer) in a multi-worker deploy, or back it with your DB.
- **Poisoning.** One mislabeled statement could teach a bad synonym. Conflict
  detection + provenance + a periodic review of `source != seed` entries mitigates.
- **Normalization.** Learn the normalized phrase (lowercased, whitespace-collapsed)
  to avoid near-duplicate entries.

## 11. What you get

- No more editing Python to add a bank.
- The AI fires less over time (each new header becomes a permanent exact match).
- A reviewable, revertable record of every learned mapping.
- A one-command way to seed the vocabulary from statements you already have.
