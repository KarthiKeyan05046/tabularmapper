# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

## [1.0.10] — 2026-07-07

### Added
- **Config-driven learn-store gating.** `gated_fields` is now a config key (and
  parsed by `config_from_dict` / emitted by `config_to_dict`); the library gates
  **nothing** by default. `bank_preset()` opts into `["debit","credit"]`, and the
  FastAPI learn store reads `gated_fields` from the active config instead of the
  previously hardcoded debit/credit.
- **Config-builder UI controls** for the config keys that had no editor:
  **Reconcile** (signed + the new direction/flag mode, with negative/positive
  fields and flag-value lists), **Gated fields** (checkboxes), and an **AI system
  prompt** textarea — all feeding the live `config.json` preview. The test page
  gains a **download-format selector** (.xlsx / .json).
- **Direction-column reconcile.** `reconcile` now supports an unsigned `amount`
  column paired with a separate direction/flag column (e.g. `Type` = DEBIT/CREDIT,
  or a `DR/CR` column): `reconcile: {signed, negative, positive, direction,
  negative_values?, positive_values?}`. The amount is routed to debit/credit by
  the flag value instead of by sign. Falls back to sign-routing when no flag
  column is present, and to separate debit/credit columns otherwise — one config
  handles all three layouts. If `direction` is declared but its column isn't
  found and every row resolves to one side, the file is flagged `needs_review`
  rather than silently booking everything one way.

## [1.0.9] — 2026-07-06

### Added
- **Bundled test-mapping page at `GET /test`.** A self-contained web page (served
  from `tabularmapper/static/test.html`, like the config builder at `/config`) to
  try the mapper against a live server: drop an `.xlsx`, tune the fuzzy threshold,
  and inspect the result — a **schema-coverage** panel showing which expected
  fields were satisfied (and from which source column) versus missing, a
  **column-mapping** table that spells out *why* each unmapped column was skipped
  (blank header / not in schema / duplicate / no confident match, with the best
  score), the mapped rows, a **learning queue** to approve/reject pending
  debit/credit synonyms inline, an **AI-ready** status indicator, and a
  **Download mapped `.xlsx`** button. The page targets the API relative to its own
  URL, so it works under any `TABULARMAPPER_ROUTE_PREFIX` with no configuration.

## [1.0.8] — 2026-07-03

### Fixed
- **Split two-row header no longer corrupts debit/credit.** When a header is
  wrapped across two physical rows (e.g. `Withdrawal | Deposit` above
  `Amount | Amount`), the engine now merges the header-fragment row directly above
  the detected header, so the two money columns map to `debit` and `credit`
  instead of collapsing to one field. Previously a withdrawal was silently booked
  as a credit and a matching credit amount was dropped. The merge is conservative:
  it only merges a row of short text-only labels spanning ≥2 columns, so titles,
  merged banners, and metadata rows are untouched.

### Added
- **Mutually-exclusive numeric dup backstop.** If two numeric columns are
  mutually exclusive across the sampled rows yet collapse to the same field (the
  debit/credit-pair signature a single-row header can hide), the loser is flagged
  `needs_review` instead of being silently dropped as a `dup`.

## [1.0.7] — 2026-07-03

### Added
- **`TABULARMAPPER_AI_FILL`** controls the AI trigger. New default **`all`**: when
  AI is enabled, it fills **any** column the deterministic pass left unmapped (so
  non-critical columns like a reference number get an AI attempt too), not just
  critical gaps. Set `critical` for the previous behaviour (AI only on a missing
  critical field). AI still runs only on uncached layouts, and the result caches.

### Changed
- **AI trigger default is now `all`** (was: critical-gap only). If AI is enabled,
  expect it to also resolve non-critical leftovers on new layouts.

## [1.0.6] — 2026-07-03

### Added
- **Configurable AI system prompt.** Override the matcher's system prompt via
  `OpenAICompatibleMatcher(system_prompt=...)`, an `ai_system_prompt` field in the
  config JSON, or the `TABULARMAPPER_AI_SYSTEM_PROMPT` env var. The JSON-output
  contract stays in the user message, so overriding is safe.

### Changed
- The AI matcher's **default system prompt is now domain-neutral** (no longer
  bank-specific) and a structured, bounded rule set: header-semantics-first,
  mutual-exclusivity for paired fields, unresolved abbreviations → `null`,
  derived/duplicate columns → `null`, and a conservative "map only what the
  evidence supports; a `null` is expected, not a failure" stance with single-pass
  reasoning. Also fixed an impossible "flag explicitly" instruction (the contract
  is field-or-`null`, so there is no flag channel).

### Docs
- Documented using any provider (Anthropic/Gemini/Kimi) via OpenRouter — one
  `base_url`, no new dependency. Removed the unused `ai_adapter.py`. Clarified
  `field: null` + AI + cache behavior in `how-it-works.md`.

## [1.0.5] — 2026-07-02

### Added
- **Config-builder web page.** `GET /mapper/config` serves a self-contained
  HTML page (`tabularmapper/static/index.html`) for designing an output schema —
  fields, types, synonyms, descriptions, `critical_fields`/`require_any`/
  `reconcile` — with a live `config.json` preview plus copy/download. Bundled in
  the wheel via package-data, so it works from a pip install, not just a checkout.
  Supports importing a `config.json` file, and a **Load current** button that
  seeds the builder from the active config via the new `GET /mapper/config.json`
  endpoint.

## [1.0.3] — 2026-07-02

### Added
- **Configurable fuzzy threshold on `/map`.** New `TABULARMAPPER_THRESHOLD` env
  var (default 80) sets the fuzzy-accept gate, and a per-request `?threshold=`
  query param (0–100) overrides it. Raising the gate pushes borderline fuzzy
  matches below it into the AI matcher instead of trusting them.

### Changed
- The mapping cache is now scoped to the threshold as well as the schema, so
  changing the gate re-evaluates a layout instead of replaying a mapping computed
  at a different threshold.

### Fixed
- Corrected stale `pip install bank-statement-mapper` hints in the Redis/Valkey/
  Postgres error messages (now `tabularmapper`), and a `BANK_MAPPER_CONFIG`
  reference in a docstring.

## [1.0.2] — 2026-07-02

### Added
- Broader Python support: classifiers now cover 3.9 through 3.14
  (`requires-python` stays `>=3.9`).

### Changed
- `/map`'s `format` parameter is now an enum, so the interactive API docs
  (`/docs`) render it as a dropdown — `json` / `base64` / `file` — instead of a
  free-text field. Behavior is unchanged.

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

[Unreleased]: https://github.com/KarthiKeyan05046/tabularmapper/compare/v1.0.8...HEAD
[1.0.8]: https://github.com/KarthiKeyan05046/tabularmapper/compare/v1.0.7...v1.0.8
[1.0.7]: https://github.com/KarthiKeyan05046/tabularmapper/compare/v1.0.6...v1.0.7
[1.0.6]: https://github.com/KarthiKeyan05046/tabularmapper/compare/v1.0.5...v1.0.6
[1.0.5]: https://github.com/KarthiKeyan05046/tabularmapper/compare/v1.0.3...v1.0.5
[1.0.3]: https://github.com/KarthiKeyan05046/tabularmapper/compare/v1.0.2...v1.0.3
[1.0.2]: https://github.com/KarthiKeyan05046/tabularmapper/compare/v1.0.1...v1.0.2
[1.0.1]: https://github.com/KarthiKeyan05046/tabularmapper/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/KarthiKeyan05046/tabularmapper/releases/tag/v1.0.0
