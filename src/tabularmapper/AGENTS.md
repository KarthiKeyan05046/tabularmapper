# tabularmapper source package

## Overview

tabularmapper is a two-stage pipeline library. The first stage detects the true header row in a spreadsheet by scoring candidate rows; the second maps each detected header to an output field by exact match, fuzzy match, cache lookup, or optional LLM call. All stages are config-driven and fail safe to the built-in bank preset.

## Key files

| File | Owns |
|---|---|
| engine.py | Full pipeline: header detection, column mapping, row extraction, date/amount normalisation |
| schema.py | Config loading from JSON file, URL, S3 URL, or dict; manages the four module-level config globals |
| ai_matcher.py | OpenAI-compatible LLM column matcher; active only when `OPENAI_API_KEY` is present |
| llm_fallback.py | Pluggable offline fallback adapters (e.g. `HashingEmbeddingFallback`); swappable at call time |
| learn.py | Self-learning synonym vocabulary; persists confirmed header to field mappings in a configurable store |
| stores.py | Pluggable key/value backends: memory, file, SQLite, Redis, Valkey, PostgreSQL; shared by cache and learn store |
| mapping_cache.py | Header fingerprint to field mapping cache; repeat bank formats skip all detection (instant 100% match) |
| cli.py | CLI entry point (`tabularmapper` command); delegates all logic to engine and schema |
| api.py | FastAPI router; mounts at `/mapper` by default; cache is built once in the lifespan handler |

## Conventions

- `configure()` swaps four module-level globals (`OUTPUT_SCHEMA`, `SYNONYMS`, `CRITICAL_FIELDS`, `_ACTIVE_CONFIG`) atomically; calling it on a running API instance is not thread-safe
- `@dataclass` is used for all data types (`ProcessResult`, `ColumnMap`, `OutputResult`, `Config`); no `frozen=True`
- Public API is exported from `__init__.py`; consumers never import from engine.py or schema.py directly
- All modules use `from __future__ import annotations` with full type hints

## Gotchas

- **Version sync**: `pyproject.toml` and `__init__.py` both carry the version string; bump both on every release (they diverged, with pyproject.toml at 1.0.15 and `__init__.py` still at 1.0.12)
- **LLM privacy invariant**: `ai_matcher.py` must receive only the header row and column names, never transaction row data; no dedicated test enforces this, it is a design-level constraint
- **Configure before process**: `configure()` or a preset call must happen before any `process_*()` call; nothing maps silently if skipped, but there is no explicit guard
- **TSV-as-XLS**: some bank exports (e.g. SBI) serve tab-delimited text with a `.xls` extension; the engine auto-detects this and re-routes through the TSV parser
- **Gated fields**: `debit` and `credit` columns in the bank preset require human confirmation before the learn store auto-applies them; other fields apply immediately on first confirmation
- **Cache fingerprint**: the header fingerprint is a hash of normalised cell strings; identical bank statement formats skip all detection on repeat runs

_Drafted by /audit from the repo, worth a quick human pass. Edit freely: once a line stops matching this draft, later runs treat it as curated and will flag rather than overwrite it._
