# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- The mapping cache is now scoped to the active schema. Previously the cache key
  was the header row only, so changing your config (e.g. adding a field) and
  reprocessing the same file returned the **stale** mapping and the new field
  stayed unmapped. The key now includes a signature of the output schema +
  synonyms + reconcile, so a config change invalidates old entries; learned
  synonyms are excluded from the signature so learning doesn't churn the cache.

### Added
- `currency` is now an accepted field `type`, equivalent to `number` / `money`
  (previously an unknown type silently fell back to `money`).

### Changed
- Column mapping only assigns fields declared in your `output_schema` (plus any
  `reconcile`/`amount` fields). A header recognized as a field you didn't declare
  is now reported with `field: null` and `method: "not_in_schema"` instead of
  showing a field that never appears in the output. Merged default synonyms no
  longer leak unrelated fields (e.g. `credit`/`balance`) into a 2-column config.

## [1.0.1] — 2026-07-02

### Documentation
- Document the runtime SQLite files created at startup — `mapping_cache.db`,
  `learned_synonyms.db`, and the `.db-wal` / `.db-shm` Write-Ahead-Logging
  sidecars — including why they appear and how to control storage via
  `BANK_MAPPER_CACHE` / `BANK_MAPPER_LEARN_STORE` (`memory://` for no files, a
  filesystem path, or a Redis/Valkey/Postgres URL). Added a matching FAQ entry.

## [1.0.0] — 2026-07-02

First public release. Two-stage, auditable pipeline: deterministic header-row
detection + synonym/fuzzy column mapping, deterministic date/amount parsing, and
a human-review gate.

### Added
- **Generic table→schema mapper.** The engine no longer hardcodes any bank field
  names. All domain behavior is config-driven: `reconcile` (split one signed
  column into two directional ones), `require_any`, `row_keep_if_any`,
  `continuation_field`, per-field `description`, and the `number` field type.
  "Bank" is now just the built-in default config (`config.example.json`).
- **Self-learning vocabulary** (`learn.py`): `LearnStore` + `learn_from_result`
  remember AI/human-confirmed headers so a new layout maps as an `exact` match
  next time. Debit/credit are gated to a review queue; everything else
  auto-applies. `harvest_folder` bootstraps the vocabulary from an archive.
- **AI table matcher** (`ai_matcher.OpenAICompatibleMatcher`): one LLM call maps
  an unknown header row from column *structure only* (types, fill rate, mutual
  exclusivity) — never transaction values. Works with any OpenAI-compatible
  endpoint. Off unless `OPENAI_API_KEY` is set.
- **Pluggable storage** (`stores.py`) via a URL convention shared by the cache
  and learn store: `memory://`, `sqlite://` (default), `redis://`, `valkey://`
  / `valkeys://` (Aiven-compatible), `postgresql://`. Drivers are optional
  extras, lazy-imported, with a friendly install message.
- **Loadable config** (`schema.py`): output template + synonyms from a JSON
  file, `https://` / `s3://` URL, or dict via `BANK_MAPPER_CONFIG` / `configure()`.
  User synonyms merge on top of the defaults (`replace_synonyms: true` to opt out).
- **Multiple output formats** via `output_format`: `records`, `json`, `bytes`,
  `base64`, `file` (`OutputResult`, lazily serialized) + `records_to_csv_bytes`.
- **In-memory processing** (`process_stream`) — parse an upload's bytes with no
  temp file (nothing written to disk).
- **FastAPI router** (`bank_mapper_api`): `POST /statements/map`,
  `GET /statements/health`, and `/statements/learn/{pending,approve,reject}`,
  with a `lifespan` that wires cache/config/learn from env vars.
- **CLI** (`bank-mapper`): `--format`, `--config`, `--cache`, `--ai`, `--learn`,
  `--harvest`, `--fallback`; optional `.env` auto-load via `python-dotenv`.
- Packaged as an installable `bank_statement_mapper` distribution (MIT license,
  `src/` layout, optional extras `[api] [redis] [valkey] [postgres] [dotenv]`).

### Changed
- Restructured from flat top-level modules into the `src/bank_statement_mapper/`
  package, so imports are `from bank_statement_mapper import ...` and there is no
  top-level namespace pollution (`import cli` / `import schema` no longer leak).
- Default mapping cache is now **SQLite** (concurrency-safe), not a JSON file.
- The FastAPI `lifespan` only calls `configure()` when `BANK_MAPPER_CONFIG` is
  set, so a manual `configure("config.json")` before startup is not overwritten.

### Fixed
- Config `synonyms` now **merge** with the built-in defaults instead of replacing
  them, so adding one phrase no longer breaks date/description matching.
- The mapping cache only persists a freshly computed mapping when it is
  trustworthy (never an unconfirmed low-confidence/fallback guess).
- A bad or unreachable config source logs a warning and falls back to defaults
  instead of failing silently.

### Security
- Optional backend drivers (redis/valkey/psycopg/boto3) and `fastapi`/`dotenv`
  are opt-in extras — the core install pulls no database driver and makes zero
  network calls. Connection URLs (with secrets) belong in env/`.env`, never code.

[Unreleased]: https://github.com/KarthiKeyan05046/bank-statement-mapper/compare/v1.0.1...HEAD
[1.0.1]: https://github.com/KarthiKeyan05046/bank-statement-mapper/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/KarthiKeyan05046/bank-statement-mapper/releases/tag/v1.0.0
