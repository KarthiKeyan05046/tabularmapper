#!/usr/bin/env python3
"""
cli.py — command-line runner for the bank statement mapper.

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
    --no-cache                            disable mapping_cache.json
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

from bank_mapper import process_file
from mapping_cache import MappingCache


def _build_fallback(kind: str):
    if kind == "none":
        return None
    if kind == "hashing":
        from llm_fallback import HashingEmbeddingFallback
        return HashingEmbeddingFallback()
    raise ValueError(kind)


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
    ap = argparse.ArgumentParser(description="Bank statement -> standard schema mapper")
    ap.add_argument("input")
    ap.add_argument("output", nargs="?", default=None)
    ap.add_argument("--format", choices=["file", "json", "bytes", "base64", "records"],
                    default="file",
                    help="output format (default: file)")
    ap.add_argument("--ai", action="store_true",
                    help="LLM table matcher for unknown headers")
    ap.add_argument("--model", default=None, help="LLM model (or env OPENAI_MODEL)")
    ap.add_argument("--fallback", choices=["none", "hashing"], default="none")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--threshold", type=int, default=80)
    args = ap.parse_args(argv)

    if not os.path.exists(args.input):
        print(f"error: input not found: {args.input}", file=sys.stderr)
        return 2

    out = args.output
    if out is None and args.format == "file":
        base, _ = os.path.splitext(args.input)
        out = base + ".standardized.xlsx"

    fallback = _build_fallback(args.fallback)
    cache = None if args.no_cache else MappingCache()

    table_matcher = None
    if args.ai:
        from ai_matcher import OpenAICompatibleMatcher
        if not os.getenv("OPENAI_API_KEY"):
            print("warning: --ai set but OPENAI_API_KEY is empty; the AI call "
                  "will fail and columns stay unmapped.", file=sys.stderr)
        table_matcher = OpenAICompatibleMatcher(model=args.model)

    res = process_file(args.input, out_path=out, output_format=args.format,
                       llm_fallback=fallback, table_matcher=table_matcher,
                       threshold=args.threshold, cache=cache)

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