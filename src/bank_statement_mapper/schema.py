"""
schema.py — externalized, loadable configuration for the mapper.

Everything that used to be a hardcoded constant in `bank_mapper.py` — the output
template (`OUTPUT_SCHEMA`), the header vocabulary (`SYNONYMS`), and the critical
fields — lives here as data, and can be loaded from a JSON file, an HTTP(S) URL,
an S3 object, or an in-memory dict. Change the template by editing JSON in a
bucket; no code change, no redeploy.

Config JSON shape (all keys optional; missing keys fall back to the defaults):

    {
      "version": 1,
      "output_schema": [
        {"field": "date",        "header": "Date",             "type": "date"},
        {"field": "description", "header": "Narration",        "type": "text"},
        {"field": "debit",       "header": "Debit",            "type": "money"},
        {"field": "credit",      "header": "Credit",           "type": "money"}
      ],
      "critical_fields": ["date"],
      "synonyms": { "date": ["date", "txn date"], "debit": ["withdrawal"] }
    }

`type` ∈ {"date", "money", "text"} drives generic extraction, so adding a NEW
column is a config-only change. The field keys `debit`, `credit`, `amount` keep
their special money-reconciliation behavior (a single signed `amount` column is
split into debit/credit).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from dataclasses import dataclass, field as _field
from typing import Optional, Union

_log = logging.getLogger("bank_mapper.schema")

# Field types the engine understands, grouped by how they're parsed. Many
# aliases so configs read naturally ("string", "integer", "currency", …).
DATE_TYPES = {"date", "datetime"}
NUMERIC_TYPES = {"money", "number", "currency", "numeric", "decimal", "float",
                 "integer", "int"}
INTEGER_TYPES = {"integer", "int"}          # coerced to int when whole
TEXT_TYPES = {"text", "string", "str"}
VALID_TYPES = DATE_TYPES | NUMERIC_TYPES | TEXT_TYPES

# --------------------------------------------------------------------------
# Defaults — copied VERBATIM from the original bank_mapper.py constants so the
# out-of-the-box behavior is byte-identical.
# --------------------------------------------------------------------------
DEFAULT_SCHEMA: list[dict] = [
    {"field": "date", "header": "Date", "type": "date",
     "description": "the transaction date (post/value/booking date)"},
    {"field": "description", "header": "Narration", "type": "text",
     "description": "free-text narration / particulars / details of the transaction"},
    {"field": "reference", "header": "Reference Number", "type": "text",
     "description": "reference or cheque/UTR/instrument number identifying the entry"},
    {"field": "debit", "header": "Debit", "type": "money",
     "description": "money leaving the account (withdrawal / paid out); a debit-only column"},
    {"field": "credit", "header": "Credit", "type": "money",
     "description": "money entering the account (deposit / paid in); a credit-only column"},
    {"field": "balance", "header": "Balance", "type": "money",
     "description": "running account balance after the transaction"},
]

DEFAULT_CRITICAL_FIELDS: list[str] = ["date"]

# --- Bank preset behavior (all data, not engine logic) -------------------
# reconcile: a single signed `amount` column is split into debit(-)/credit(+);
#   when debit/credit are their own columns they're taken as positive.
DEFAULT_RECONCILE: dict = {"signed": "amount", "negative": "debit", "positive": "credit"}
# require_any: each group needs >=1 mapped field or the statement is flagged.
DEFAULT_REQUIRE_ANY: list = [["debit", "credit", "amount"]]
# row_keep_if_any: a row is a real record if >=1 of these has a value.
DEFAULT_ROW_KEEP_IF_ANY: list = ["date", "debit", "credit"]
# continuation_field: a row with only this field folds into the row above it.
DEFAULT_CONTINUATION_FIELD: Optional[str] = "description"
# descriptions for fields the AI matcher may see but that aren't output columns
DEFAULT_FIELD_DESCRIPTIONS: dict = {
    "amount": "a SINGLE signed amount column (one column, +credit / -debit)",
}

DEFAULT_SYNONYMS: dict[str, list[str]] = {
    "date": [
        "date", "txn date", "transaction date", "value date", "posting date",
        "post date", "tran date", "date of transaction", "trans date", "dt",
        "booking date", "entry date",
    ],
    "description": [
        "description", "narration", "particulars", "details", "remarks",
        "transaction details", "transaction remarks", "narrative", "memo",
        "transaction description", "txn description", "notes", "purpose",
    ],
    "reference": [
        "reference", "reference number", "reference no", "ref no", "ref no.",
        "ref no./cheque no", "ref no./cheque no.", "cheque no", "cheque no.",
        "chq no", "chq no.", "ref", "reference id", "utr", "utr no",
        "instrument no", "cheque/ref no", "chq/ref no", "transaction id",
        "ref/cheque no",
    ],
    "debit": [
        "debit", "withdrawal", "withdrawals", "withdrawal amt", "withdrawal amount",
        "withdrawal (dr)", "dr", "dr amount", "debit amount", "debit amt",
        "paid out", "payments", "money out", "amount debited", "outflow",
        "debit(dr)", "withdrawal amt.",
    ],
    "credit": [
        "credit", "deposit", "deposits", "deposit amt", "deposit amount",
        "deposit (cr)", "cr", "cr amount", "credit amount", "credit amt",
        "paid in", "receipts", "money in", "amount credited", "inflow",
        "credit(cr)", "deposit amt.",
    ],
    "balance": [
        "balance", "closing balance", "running balance", "available balance",
        "balance amount", "bal", "closing bal", "ledger balance", "book balance",
        "balance (inr)",
    ],
    "amount": [
        "amount", "transaction amount", "txn amount", "amt", "value",
        "signed amount", "amount (inr)", "amount(dr/cr)", "transaction amt",
    ],
}


# --------------------------------------------------------------------------
# Data classes
# --------------------------------------------------------------------------
@dataclass
class FieldSpec:
    field: str                 # internal key: date, description, debit, ...
    header: str                # display name written to the output file
    type: str = "text"         # date | number/money | text
    description: str = ""       # optional; used by the AI matcher


@dataclass
class Config:
    output_schema: list[FieldSpec]
    synonyms: dict[str, list[str]]
    critical_fields: list[str]
    # domain behavior — all data-driven, empty by default for a generic mapper
    reconcile: dict = _field(default_factory=dict)          # {signed,negative,positive}
    require_any: list = _field(default_factory=list)        # [[field, ...], ...]
    row_keep_if_any: list = _field(default_factory=list)    # keep row if any has a value
    continuation_field: Optional[str] = None                # multi-line fold target
    extra_field_descriptions: dict = _field(default_factory=dict)  # non-output field defs

    # -- derived views the engine consumes --
    @property
    def fields(self) -> list[str]:
        return [f.field for f in self.output_schema]

    @property
    def headers(self) -> list[tuple[str, str]]:
        """Back-compat shape: list of (field_key, display_header)."""
        return [(f.field, f.header) for f in self.output_schema]

    @property
    def field_types(self) -> dict[str, str]:
        return {f.field: f.type for f in self.output_schema}

    @property
    def field_descriptions(self) -> dict[str, str]:
        """{field: description} for the AI matcher (output fields + extras)."""
        out = {f.field: (f.description or f.field) for f in self.output_schema}
        out.update(self.extra_field_descriptions)
        return out

    @property
    def reconcile_fields(self) -> list[str]:
        """The fields involved in signed/split reconciliation, if any."""
        r = self.reconcile or {}
        return [r[k] for k in ("signed", "negative", "positive") if r.get(k)]

    @property
    def allowed_fields(self) -> list[str]:
        fs = list(self.fields)
        for extra in list(self.extra_field_descriptions) + self.reconcile_fields:
            if extra not in fs:
                fs.append(extra)
        return fs


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------
def _infer_type(field_key: str) -> str:
    if field_key == "date":
        return "date"
    if field_key in {"debit", "credit", "balance", "amount"}:
        return "money"
    return "text"


def default_config() -> Config:
    return Config(
        output_schema=[FieldSpec(**d) for d in DEFAULT_SCHEMA],
        synonyms={k: list(v) for k, v in DEFAULT_SYNONYMS.items()},
        critical_fields=list(DEFAULT_CRITICAL_FIELDS),
        reconcile=dict(DEFAULT_RECONCILE),
        require_any=[list(g) for g in DEFAULT_REQUIRE_ANY],
        row_keep_if_any=list(DEFAULT_ROW_KEEP_IF_ANY),
        continuation_field=DEFAULT_CONTINUATION_FIELD,
        extra_field_descriptions=dict(DEFAULT_FIELD_DESCRIPTIONS),
    )


def config_from_dict(d: dict, _origin: str = "<dict>") -> Config:
    """Build a Config from a parsed JSON dict. Missing keys use defaults."""
    if not d.get("output_schema"):
        # loaded successfully but no schema declared -> you'll get the DEFAULT
        # columns (incl. balance). Usually a typo'd key or empty list.
        _log.warning(
            "config %s has no non-empty 'output_schema' — using the DEFAULT "
            "output columns (Date, Narration, Reference Number, Debit, Credit, "
            "Balance). Check the key name.", _origin)
    specs: list[FieldSpec] = []
    for item in d.get("output_schema") or DEFAULT_SCHEMA:
        if isinstance(item, dict):
            key = item["field"]
            specs.append(FieldSpec(
                field=key,
                header=item.get("header", key),
                type=item.get("type") or _infer_type(key),
                description=item.get("description", ""),
            ))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            specs.append(FieldSpec(field=item[0], header=item[1],
                                   type=_infer_type(item[0])))
    for s in specs:
        if s.type not in VALID_TYPES:
            s.type = _infer_type(s.field)
    # MERGE user synonyms on top of the built-in defaults (don't replace them),
    # so adding one phrase doesn't wipe out date/description/etc. matching.
    # Set "replace_synonyms": true to start from an empty vocabulary instead.
    if d.get("replace_synonyms"):
        syn = {k: list(v) for k, v in (d.get("synonyms") or {}).items()}
    else:
        syn = {k: list(v) for k, v in DEFAULT_SYNONYMS.items()}
        for fld, phrases in (d.get("synonyms") or {}).items():
            base = syn.setdefault(fld, [])
            for p in phrases:
                if p not in base:
                    base.append(p)
    # Generic default: no critical fields unless declared (the bank preset in
    # default_config() supplies ["date"]). This keeps non-bank configs from
    # inheriting a bank-specific "date is required" rule.
    crit = d.get("critical_fields") or []
    # Domain behavior defaults to EMPTY (generic mapper). Declare these keys to
    # opt into bank-style reconciliation etc. — the built-in default config (used
    # when no config is provided) supplies the bank preset.
    return Config(
        output_schema=specs or [FieldSpec(**x) for x in DEFAULT_SCHEMA],
        synonyms=syn,
        critical_fields=list(crit),
        reconcile=dict(d.get("reconcile") or {}),
        require_any=[list(g) for g in (d.get("require_any") or [])],
        row_keep_if_any=list(d.get("row_keep_if_any") or []),
        continuation_field=d.get("continuation_field"),
        extra_field_descriptions=dict(d.get("field_descriptions") or {}),
    )


# --------------------------------------------------------------------------
# Loading — file / http(s) / s3 / dict, with a fail-safe to defaults
# --------------------------------------------------------------------------
def _read_source(source: str, timeout: float = 10.0) -> bytes:
    if source.startswith("s3://"):
        return _read_s3(source)
    if source.startswith(("http://", "https://")):
        with urllib.request.urlopen(source, timeout=timeout) as resp:
            return resp.read()
    if source.startswith("file://"):
        source = source[len("file://"):]
    with open(source, "rb") as fh:
        return fh.read()


def _read_s3(uri: str) -> bytes:
    from urllib.parse import urlparse
    try:
        import boto3  # optional; only for s3:// sources — or use a presigned https URL
    except ImportError as exc:
        raise ImportError(
            "Loading config from s3:// needs the 'boto3' package (pip install "
            "boto3), or pass a presigned https:// URL instead (no dependency)."
        ) from exc
    parts = urlparse(uri)
    obj = boto3.client("s3").get_object(Bucket=parts.netloc,
                                        Key=parts.path.lstrip("/"))
    return obj["Body"].read()


def load_config(source: Optional[Union[str, dict]] = None,
                strict: bool = False) -> Config:
    """Load configuration.

    source:
      * None      -> env BANK_MAPPER_CONFIG, else the built-in defaults
      * dict      -> used directly
      * "s3://…"  -> S3 object (needs boto3) OR use a presigned https URL instead
      * "http(s)://…" / path / "file://…" -> fetched via stdlib urllib

    On any load/parse error, falls back to the defaults (so a bad or unreachable
    config never takes the service down) unless `strict=True`.
    """
    if source is None:
        source = os.getenv("BANK_MAPPER_CONFIG")
    if source is None:
        return default_config()
    if isinstance(source, dict):
        return config_from_dict(source)
    try:
        raw = _read_source(str(source))
        return config_from_dict(json.loads(raw), _origin=str(source))
    except Exception as exc:
        if strict:
            raise
        _log.warning(
            "BANK_MAPPER config %r failed to load (%s: %s) — falling back to "
            "built-in defaults", source, type(exc).__name__, exc)
        return default_config()


def config_to_dict(cfg: Config) -> dict:
    """Serialize a Config back to the JSON-friendly shape (for saving/harvest)."""
    return {
        "version": 1,
        "output_schema": [
            {"field": f.field, "header": f.header, "type": f.type,
             **({"description": f.description} if f.description else {})}
            for f in cfg.output_schema
        ],
        "critical_fields": list(cfg.critical_fields),
        "reconcile": dict(cfg.reconcile),
        "require_any": [list(g) for g in cfg.require_any],
        "row_keep_if_any": list(cfg.row_keep_if_any),
        "continuation_field": cfg.continuation_field,
        "field_descriptions": dict(cfg.extra_field_descriptions),
        "synonyms": {k: list(v) for k, v in cfg.synonyms.items()},
    }
