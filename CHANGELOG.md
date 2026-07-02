# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

## [1.0.1] — 2026-07-02

### Added
- **`/map` output formats.** The FastAPI endpoint takes a `?format=` query
  param: `json` (default, unchanged), `base64` (the usual response plus a mapped
  `.xlsx` in `file_base64`), and `file` (the `.xlsx` streamed back as a download,
  no JSON body). The spreadsheet bytes are built lazily, so the default JSON path
  is unaffected.

## [1.0.0] — 2026-07-02

Initial release. A general spreadsheet (`.xlsx`) → schema mapper: it finds the
header row, maps columns to a schema you define, parses values deterministically,
and flags anything ambiguous for review. Bank statements are a built-in preset —
the engine itself is domain-agnostic.

### Added
- **Deterministic pipeline.** Header-row detection (scoring, no AI), exact +
  fuzzy column matching, type-aware value parsing (dates never day/month-flipped;
  amounts with commas, currency symbols, `Dr`/`Cr`, parentheses), and a
  `needs_review` gate — a model never sees a data row.
- **Config-driven, no hardcoded fields.** Declare `output_schema` (types `date`,
  `number`/`money`/`currency`/`integer`/`float`, `text`/`string`), `synonyms`,
  and optional `critical_fields`, `require_any`, `reconcile` (split one signed
  column into two directional ones), `row_keep_if_any`, `continuation_field`.
  Load from a file / `https://` / `s3://` / dict via `TABULARMAPPER_CONFIG` or
  `configure()`. `bank_preset()` (also `config.example.json`) is the ready-made
  bank layout; the mapping cache is scoped to the active schema.
- **Optional AI column matcher** (`OpenAICompatibleMatcher`) for unknown headers —
  sends column *structure* only (types, fill rate, mutual exclusivity), never
  cell data. Works with any OpenAI-compatible endpoint; off unless
  `OPENAI_API_KEY` is set.
- **Self-learning vocabulary** (`LearnStore`, `learn_from_result`,
  `harvest_folder`): AI/human-confirmed headers become exact matches; sensitive
  fields are gated to a review queue.
- **Pluggable storage** via one URL convention (`open_store`): `memory://`
  (default — no files), `sqlite://`, `redis://`, `valkey://` / `valkeys://`
  (Aiven-compatible), `postgresql://`. Drivers are optional extras.
- **Multiple output formats** (`records`, `json`, `bytes`, `base64`, `file`) via
  `OutputResult`, plus `records_to_csv_bytes` and in-memory `process_stream`
  (no temp file).
- **FastAPI router** with a configurable prefix (default `/mapper`, or
  `make_router(...)` / `TABULARMAPPER_ROUTE_PREFIX`) and a `lifespan` that wires
  cache/config/learn from env vars.
- **CLI** `tabularmapper` with `--config`, `--preset bank`, `--cache`, `--ai`,
  `--learn`, `--harvest`, `--format`; optional `.env` auto-load.
- MIT licensed, installable package (`pip install tabularmapper`; extras
  `[api] [redis] [valkey] [postgres] [dotenv]`).

[Unreleased]: https://github.com/KarthiKeyan05046/tabularmapper/compare/v1.0.1...HEAD
[1.0.1]: https://github.com/KarthiKeyan05046/tabularmapper/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/KarthiKeyan05046/tabularmapper/releases/tag/v1.0.0
