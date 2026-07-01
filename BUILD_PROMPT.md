# Build Prompt — Bank Statement → Standard Schema Mapper

> Paste this whole file into Claude Code as the project brief. It defines what to
> build, the hard constraints, the architecture, edge cases, and how "done" is
> judged. A working reference implementation (`bank_mapper.py`) already exists and
> passed tests — treat it as the proven core to productionize, not a throwaway.

---

## 1. Goal (one sentence)

Take a bank statement in `.xlsx` from **any bank, in any layout**, and produce a
standardized `.xlsx` with a fixed set of columns — with the field-mapping done
reliably and every unrecognized format flagged for human review.

## 2. The problem, stated precisely

Bank statements are NOT uniform:

- The transaction table's **header row is not always row 1**. Statements have bank
  logos, account info, and "Statement Period …" lines above the real header.
- **Column names differ per bank**: "Narration" vs "Particulars" vs "Description";
  "Withdrawal" vs "Debit" vs "Dr".
- Money is represented **two incompatible ways**: (a) separate Debit + Credit
  columns, or (b) one signed Amount column (`-1299`, `3500 Dr`, `(299)`).
- Dates come as Excel serials, `dd-mm-yyyy`, `yyyy/mm/dd`, or `12 Jun 2026`.

Therefore the job splits into two stages that MUST stay separate.

## 3. Hard constraints (do NOT violate)

1. **Two-stage pipeline.** Stage 1 = *detect the header row*. Stage 2 = *map that
   row's columns to output fields*. Never assume row 1 is the header.
2. **No LLM ever touches transaction data.** The model may only see column header
   strings + at most 3 sample cells per column. All row/amount/date processing is
   deterministic Python. This is for auditability and data privacy (bank data).
3. **Header detection is deterministic scoring, not AI.** No model call to find the
   header row.
4. **LLM is a fallback only.** It fires solely for a header the synonym+fuzzy
   matcher cannot place at >= confidence threshold. Default build runs with zero
   API calls.
5. **Human-review gate is mandatory.** Any statement with a missing critical field
   (date, and one of debit/credit/amount) or a low-confidence column must be
   flagged `needs_review = True`. Do not silently best-guess financial data.
6. **Every mapping decision is logged** with method (exact/fuzzy/llm) and a 0–100
   confidence, so a human can audit why a column mapped the way it did.

## 4. Output schema (configurable, default below)

```
date | description | reference | debit | credit | balance
```

- `date` → normalized to `YYYY-MM-DD`.
- `debit` / `credit` → positive floats, on the correct side. If the source uses a
  single signed Amount column, split it: negative → debit, positive → credit.
- Keep this as a single editable constant so the user can change output fields.

## 5. Architecture / required functions

```
detect_header_row(rows, scan_limit=25) -> HeaderCandidate
    Score each of the first ~25 rows on:
      + density (many non-empty cells)
      + text ratio (headers are words, not numbers/dates)
      + short labels
      + banking-vocabulary hits   <-- strongest signal
      + rows BELOW look like data (numbers/dates)
      - penalty if the row itself is mostly numbers/dates
    Return the highest-scoring row + its score breakdown.

map_columns(header_row, sample_rows, llm_fallback=None, threshold=80)
    For each header cell:
      1. exact synonym match  -> confidence 100
      2. fuzzy match (rapidfuzz) -> confidence = score
      3. if still < threshold AND llm_fallback provided -> call it
    Return ColumnMap[] with {col_index, raw_header, field, confidence, method}.

normalize_date(v)    -> 'YYYY-MM-DD'  (year-first formats skip dayfirst heuristic)
normalize_amount(v)  -> signed float  (handles '1,200.50', '(500)', '500 Dr', '₹500 Cr')

extract_records(rows, header_idx, col_maps) -> list[dict]
    Reconcile split debit/credit vs single signed amount. Skip non-transaction rows.

process_file(path, out_path=None, llm_fallback=None) -> ProcessResult
    Glue. Reads sheet, detects header, maps, extracts, sets needs_review, writes xlsx.
```

`SYNONYMS` dictionary (target field -> list of real-world header phrases) is the
primary accuracy engine. Seed it generously; make it trivial to extend.

## 6. Edge cases the build MUST handle

- Header row anywhere in the first ~25 rows (title/metadata/blank rows above it).
- Single signed Amount column AND separate Debit/Credit columns.
- Amounts with: thousands separators, currency symbols, `Dr`/`Cr` suffixes,
  parentheses-for-negative, leading minus.
- Dates: Excel datetime objects, `dd-mm-yyyy`, `dd/mm/yyyy`, `yyyy/mm/dd`,
  `12 Jun 2026`. **Known unsolvable case:** `02/06` day-vs-month ambiguity — resolve
  via a per-bank/locale setting, never silently guess. Surface it in review.
- Multi-line descriptions spilling into blank cells below a transaction.
- Multiple tables in one sheet — prefer the table whose header matches txn vocab.
- Opening/closing-balance rows with no debit/credit.
- Completely unknown format → `needs_review=True`, do not crash.

## 7. Tech stack

- Python 3.10+
- `openpyxl` (read/write xlsx), `python-dateutil` (dates), `rapidfuzz` (fuzzy match).
- LLM fallback: pluggable interface `llm_fallback(header, samples, allowed_fields) -> field|None`.
  Provide ONE concrete adapter (small model — Claude Haiku / GPT-4o-mini / Gemini
  Flash) but keep it behind the interface so it's swappable and off by default.
- Optional stretch: local embedding fallback (`bge-small` / `all-MiniLM`) for a
  fully on-prem, no-API option — cosine similarity between header and field names.

## 8. Deliverables

1. `bank_mapper.py` — the engine (functions in §5).
2. `cli.py` — `python cli.py <input.xlsx> [output.xlsx]` printing the header row
   found, the full column mapping with confidences, transaction count, and any
   review flags.
3. `mapping_cache.json` (stretch) — cache `{header_fingerprint: field_mapping}` so a
   repeat bank skips detection/mapping entirely (true 100% on seen formats).
4. `test_statements/` — at least 4 synthetic xlsx covering: junk-rows-on-top +
   split columns; title row + signed Amount; header-on-row-1 + bracket debits; an
   unknown/garbage format that must trip `needs_review`.
5. `tests/` — pytest asserting, for each fixture: correct header index, correct
   field-per-column, correct debit/credit split, correct normalized dates, and the
   right `needs_review` value.
6. `README.md` — how to run, how to add a new bank to `SYNONYMS`, how to enable the
   LLM fallback, and the human-review workflow.

## 9. Acceptance criteria (definition of done)

- [ ] All 4+ fixtures map every column to the correct field with no manual hints.
- [ ] Signed-amount statements split into debit/credit correctly.
- [ ] All date formats normalize to `YYYY-MM-DD`; year-first dates are NOT flipped.
- [ ] The garbage-format fixture produces `needs_review=True` and does not crash.
- [ ] Running with `llm_fallback=None` makes **zero** network calls.
- [ ] `pytest` is green.
- [ ] README explains adding a new bank in under 5 minutes.

## 10. Build order (suggested)

1. `detect_header_row` + its scoring, with the junk-rows fixture.
2. `SYNONYMS` + `map_columns` (exact + fuzzy only).
3. `normalize_date` / `normalize_amount` + `extract_records` (both money layouts).
4. `process_file` + `needs_review` logic + CLI.
5. Tests + fixtures, then wire the LLM fallback behind the interface.
6. (Stretch) mapping cache, then on-prem embedding fallback.

## 11. Explicitly out of scope (unless asked)

- PDF/CSV/scanned statements (this is xlsx-only for v1).
- A web UI or database — v1 is a library + CLI.
- Auto-categorizing transactions (groceries/salary/etc.).

---

### Note to the builder
"100% accuracy" is not a property any model provides. This design gets there by
making the common path deterministic (cache + synonyms + fuzzy) and routing only
the genuinely ambiguous cases to a human. Preserve that split; do not replace it
with "just ask the LLM for everything."
