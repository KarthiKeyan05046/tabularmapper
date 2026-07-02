#!/usr/bin/env python3
"""
cli.py — command-line runner for tabularmapper.

    python cli.py <input.xlsx> [output.xlsx] [options]

Options:
    --format {file,json,bytes,base64,records}
                                          output format (default: file)
    --ai                                  use the LLM table matcher for unknown
                                          headers (OpenAI-compatible; structure
                                          only, never transaction data)
    --model NAME                          LLM model (or env OPENAI_MODEL)
    --fallback {none,hashing}             offline per-column fallback
                                          (default: none -> zero network calls)
    --config PATH                         output template + synonyms JSON
                                          (file / URL / s3://; or env TABULARMAPPER_CONFIG)
    --cache URL                           cache backend: sqlite:/// | redis:// |
                                          postgresql:// | memory:// (or env
                                          TABULARMAPPER_CACHE; use env for secrets)
    --no-cache                            disable the mapping cache
    --learn [URL]                         enable self-learning (store URL optional;
                                          env TABULARMAPPER_LEARN_STORE / sqlite default)
    --harvest DIR                         seed the learned vocabulary from a folder
                                          of .xlsx statements, then exit
    --threshold N                         fuzzy confidence gate (default 80)

Env for --ai: OPENAI_API_KEY, OPENAI_BASE_URL (default OpenAI), OPENAI_MODEL.
Works with any OpenAI-compatible endpoint (OpenAI, Azure, vLLM, Ollama, ...).

Prints: the detected header row, full column mapping with confidences and
method, transaction count, and any review flags.

Output format notes:
  file    — writes .xlsx to disk (original behavior)
  json    — prints JSON string to stdout
  bytes   — writes raw .xlsx bytes to stdout (pipe to file: > out.xlsx)
  base64  — prints base64-encoded .xlsx string to stdout
  records — prints Python repr of the records list
"""

from __future__ import annotations

import argparse
import os
import sys

from .engine import process_file
from .mapping_cache import MappingCache


def _build_fallback(kind: str):
    if kind == "none":
        return None
    if kind == "hashing":
        from .llm_fallback import HashingEmbeddingFallback
        return HashingEmbeddingFallback()
    raise ValueError(kind)


def _maybe_matcher(args):
    from .ai_matcher import OpenAICompatibleMatcher
    if not os.getenv("OPENAI_API_KEY"):
        print("warning: --ai set but OPENAI_API_KEY is empty; the AI call "
              "will fail and columns stay unmapped.", file=sys.stderr)
    return OpenAICompatibleMatcher(model=args.model)


def _write_output_file(res, out_path: str) -> None:
    """Write the result to disk in the requested format."""
    fmt = res.output.format
    if fmt == "file":
        # Already written by process_file; just confirm
        print(f"  written: {out_path}")
    elif fmt == "json":
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(res.output.json)
        print(f"  written: {out_path}")
    elif fmt == "bytes":
        with open(out_path, "wb") as f:
            f.write(res.output.bytes)
        print(f"  written: {out_path}")
    elif fmt == "base64":
        with open(out_path, "w", encoding="ascii") as f:
            f.write(res.output.base64)
        print(f"  written: {out_path}")
    elif fmt == "records":
        import json
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(res.output.records, f, indent=2, ensure_ascii=False)
        print(f"  written: {out_path}")


def main(argv=None) -> int:
    # Auto-load a local .env if python-dotenv is available (optional convenience),
    # so TABULARMAPPER_CACHE / _CONFIG / _LEARN_STORE / OPENAI_* are picked up
    # without exporting. No-op if the package isn't installed.
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    ap = argparse.ArgumentParser(description="Map any spreadsheet (.xlsx) to a schema you define")
    ap.add_argument("input", nargs="?", default=None,
                    help="input .xlsx (omit when using --harvest)")
    ap.add_argument("output", nargs="?", default=None)
    ap.add_argument("--format", choices=["file", "json", "bytes", "base64", "records"],
                    default="file",
                    help="output format (default: file)")
    ap.add_argument("--ai", action="store_true",
                    help="LLM table matcher for unknown headers")
    ap.add_argument("--model", default=None, help="LLM model (or env OPENAI_MODEL)")
    ap.add_argument("--fallback", choices=["none", "hashing"], default="none")
    ap.add_argument("--config", default=None,
                    help="config JSON (file / URL / s3://); or env TABULARMAPPER_CONFIG")
    ap.add_argument("--cache", default=None,
                    help="cache backend URL: sqlite:///f.db | redis://… | "
                         "postgresql://… | memory:// (or env TABULARMAPPER_CACHE). "
                         "Prefer the env var for URLs containing secrets.")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--learn", nargs="?", const="", default=None,
                    help="enable self-learning; optional store URL "
                         "(or env TABULARMAPPER_LEARN_STORE / sqlite default)")
    ap.add_argument("--harvest", default=None, metavar="DIR",
                    help="bootstrap the learned vocabulary from a folder of "
                         ".xlsx statements, then exit")
    ap.add_argument("--preset", choices=["bank"], default=None,
                    help="use a built-in preset instead of a config (e.g. bank)")
    ap.add_argument("--threshold", type=int, default=80)
    args = ap.parse_args(argv)

    # Load the schema: --preset, then --config / TABULARMAPPER_CONFIG. With none
    # of these, the default is EMPTY and nothing is mapped.
    from .engine import configure, apply_learned
    if args.preset == "bank":
        from .schema import bank_preset
        configure(config=bank_preset())
    else:
        configure(args.config or os.getenv("TABULARMAPPER_CONFIG"))

    # Learning: enabled by --learn or --harvest. `--learn` with no value uses the
    # env/sqlite default; `--learn URL` overrides.
    learn_store = None
    if args.learn is not None or args.harvest:
        from .learn import LearnStore
        learn_store = LearnStore(args.learn or None)
        apply_learned(learn_store)

    if args.harvest:
        from .learn import harvest_folder
        matcher = _maybe_matcher(args) if args.ai else None
        report = harvest_folder(args.harvest, learn_store, table_matcher=matcher)
        print(f"Harvested {report['files']} file(s) from {args.harvest}")
        print(f"  learned : {len(report['learned'])}")
        print(f"  pending : {len(report['pending'])}  (debit/credit await approval)")
        print(f"  conflicts: {len(report['conflict'])}   errors: {len(report['errors'])}")
        print(f"  store stats: {report['stats']}")
        return 0

    if not args.input:
        print("error: input file required (or use --harvest DIR)", file=sys.stderr)
        return 2
    if not os.path.exists(args.input):
        print(f"error: input not found: {args.input}", file=sys.stderr)
        return 2

    out = args.output
    if out is None and args.format == "file":
        base, _ = os.path.splitext(args.input)
        out = base + ".standardized.xlsx"

    fallback = _build_fallback(args.fallback)
    # args.cache is None unless --cache is passed; MappingCache(None) then falls
    # back to TABULARMAPPER_CACHE or the sqlite default. Never hardcode a secret URL.
    cache = None if args.no_cache else MappingCache(args.cache)

    table_matcher = _maybe_matcher(args) if args.ai else None

    res = process_file(args.input, out_path=out, output_format=args.format,
                       llm_fallback=fallback, table_matcher=table_matcher,
                       threshold=args.threshold, cache=cache,
                       learn_store=learn_store)

    print(f"\nInput : {res.input_path}")
    print(f"Output: {res.output_path}")
    print(f"\nHeader row detected at index {res.header_index} "
          f"(score {res.header_score}) — 0-based")
    print(f"  breakdown: {res.header_breakdown}")
    hdr = [str(c) if c is not None else "" for c in
           (res.column_maps and [m.raw_header for m in res.column_maps])]
    print(f"  cells: {hdr}")

    print("\nColumn mapping:")
    print(f"  {'col':>3}  {'raw header':<28} {'-> field':<14} {'conf':>4}  method")
    for m in res.column_maps:
        fld = m.field if m.field else "(unmapped)"
        print(f"  {m.col_index:>3}  {m.raw_header[:28]:<28} {fld:<14} "
              f"{m.confidence:>4}  {m.method}")

    print(f"\nTransactions extracted: {len(res.records)}")
    if res.records:
        r = res.records[0]
        print(f"  first: {r}")

    # Output the serialized result
    print(f"\nOutput format: {args.format}")
    if args.format == "json":
        print(res.output.json)
    elif args.format == "base64":
        print(res.output.base64)
    elif args.format == "bytes":
        # Write raw bytes to stdout (binary)
        sys.stdout.buffer.write(res.output.bytes)
    elif args.format == "records":
        import json
        print(json.dumps(res.output.records, indent=2, ensure_ascii=False))
    elif out:
        # file format — already written by process_file
        print(f"  written: {out}")

    if res.needs_review:
        print("\n⚠  NEEDS REVIEW:")
        for reason in res.review_reasons:
            print(f"   - {reason}")
    else:
        print("\n✓  Clean — no review flags.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())