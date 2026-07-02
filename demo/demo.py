#!/usr/bin/env python3
"""
tabularmapper — live demo.

Run:  python demo/demo.py         (from the repo root, package installed or on PYTHONPATH)

It builds two *messy* spreadsheets on the fly — junk rows on top, non-standard
column names, amounts with Dr/Cr and brackets — then maps both into ONE clean
schema without a single line of per-file code. That's the whole pitch: a new
layout maps itself.
"""

import os
import sys
import tempfile

# Let the demo run straight from a git checkout (src/ layout) without installing.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from openpyxl import Workbook

from tabularmapper import bank_preset, configure, process_file


def _c(text, code):
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text


def bold(t):  return _c(t, "1")
def green(t): return _c(t, "32")
def cyan(t):  return _c(t, "36")
def dim(t):   return _c(t, "2")


# --------------------------------------------------------------------------
# Two files, same bank, totally different layouts — this is the real-world mess.
# --------------------------------------------------------------------------
def build_file_a(path):
    """Bank A: 4 junk rows on top, split Withdrawal/Deposit columns."""
    wb = Workbook(); ws = wb.active
    ws.append(["ACME BANK LTD."])
    ws.append(["Account Statement — Jun 2026"])
    ws.append(["Account No: XXXX1234"])
    ws.append([])  # blank
    ws.append(["Txn Date", "Narration", "Cheque No", "Withdrawal", "Deposit", "Balance"])
    ws.append(["01-06-2026", "UPI/Coffee Day", "",       "150.00", "",        "9,850.00"])
    ws.append(["02-06-2026", "Salary Credit",  "",       "",        "45,000.00", "54,850.00"])
    ws.append(["03-06-2026", "Rent Payment",   "100234", "12,000.00", "",      "42,850.00"])
    wb.save(path)


def build_file_b(path):
    """Bank B: a title row, ONE signed Amount column instead of two."""
    wb = Workbook(); ws = wb.active
    ws.append(["Monthly Transactions"])
    ws.append(["Date", "Particulars", "Ref No.", "Amount", "Running Balance"])
    ws.append(["2026/06/01", "Grocery Store",  "R-1", "-2,300.00", "7,700.00"])
    ws.append(["2026/06/02", "Refund",         "R-2", "500.00",    "8,200.00"])
    ws.append(["2026/06/03", "ATM Withdrawal",  "R-3", "3000 Dr",   "5,200.00"])
    wb.save(path)


def show(title, path):
    res = process_file(path)
    print("\n" + bold(cyan(f"═══ {title} ═══")))
    print(dim(f"file: {os.path.basename(path)}"))
    print(f"  header row auto-detected at line: {bold(str(res.header_index + 1))}")

    mapped = {m.field: m for m in res.column_maps if m.field}
    print("  column mapping (raw header → schema field):")
    for m in res.column_maps:
        if m.field:
            print(f"    {m.raw_header!r:>22}  →  {green(m.field):<14} "
                  + dim(f"[{m.method}, conf {m.confidence}]"))
        else:
            print(dim(f"    {m.raw_header!r:>22}  →  (ignored — not in schema)"))

    print(f"  needs_review: {res.needs_review}")
    print("  cleaned records (ready for JSON / DB):")
    for r in res.records:
        row = {k: v for k, v in r.items() if v not in (None, "")}
        print("    " + dim(str(row)))
    return res


def main():
    # Activate the built-in bank layout. Swap this for configure(config_from_dict(...))
    # to map ANY domain (invoices, product catalogs, HR exports — no code changes).
    configure(config=bank_preset())

    print(bold("\ntabularmapper — two messy files, one clean schema, zero per-file code\n"))
    print("Watch the SAME code handle a split debit/credit layout AND a single")
    print("signed-amount layout — junk rows, Dr/Cr, brackets and all.")

    with tempfile.TemporaryDirectory() as d:
        a = os.path.join(d, "acme_bank_statement.xlsx")
        b = os.path.join(d, "other_bank_export.xlsx")
        build_file_a(a); build_file_b(b)

        show("Bank A — split Withdrawal / Deposit, 4 junk rows on top", a)
        show("Bank B — single signed Amount column, title row on top", b)

    print(green(bold("\n✔ Both mapped to {date, description, reference, debit, credit, balance} "
                     "— no layout-specific code.\n")))
    print(dim("Same call also returns JSON, base64, or a downloadable .xlsx via the "
              "FastAPI /map?format= endpoint.\n"))


if __name__ == "__main__":
    main()
