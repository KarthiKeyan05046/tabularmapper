"""
schema.py — externalized, loadable configuration for the mapper.

Everything that used to be a hardcoded constant in `engine.py` — the output
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

_log = logging.getLogger("engine.schema")

# Field types the engine understands, grouped by how they're parsed. Many
# aliases so configs read naturally ("string", "integer", "currency", …).
DATE_TYPES = {"date", "datetime"}
NUMERIC_TYPES = {"money", "number", "currency", "numeric", "decimal", "float",
                 "integer", "int"}
INTEGER_TYPES = {"integer", "int"}          # coerced to int when whole
TEXT_TYPES = {"text", "string", "str"}
VALID_TYPES = DATE_TYPES | NUMERIC_TYPES | TEXT_TYPES

# --------------------------------------------------------------------------
# Defaults — copied VERBATIM from the original engine.py constants so the
# out-of-the-box behavior is byte-identical.
# --------------------------------------------------------------------------
BANK_SCHEMA: list[dict] = [
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

BANK_CRITICAL_FIELDS: list[str] = ["date"]

# --- Bank preset behavior (all data, not engine logic) -------------------
# reconcile: a single signed `amount` column is split into debit(-)/credit(+);
#   when debit/credit are their own columns they're taken as positive.
BANK_RECONCILE: dict = {"signed": "amount", "negative": "debit", "positive": "credit"}
# require_any: each group needs >=1 mapped field or the statement is flagged.
BANK_REQUIRE_ANY: list = [["debit", "credit", "amount"]]
# row_keep_if_any: a row is a real record if >=1 of these has a value.
BANK_ROW_KEEP_IF_ANY: list = ["date", "debit", "credit"]
# continuation_field: a row with only this field folds into the row above it.
BANK_CONTINUATION_FIELD: Optional[str] = "description"
# descriptions for fields the AI matcher may see but that aren't output columns
BANK_FIELD_DESCRIPTIONS: dict = {
    "amount": "a SINGLE signed amount column (one column, +credit / -debit)",
}

BANK_SYNONYMS: dict[str, list[str]] = {
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
    """The built-in default: EMPTY. This is a general mapper, so with no config
    it maps nothing — you must provide an output_schema + synonyms (a file/URL via
    TABULARMAPPER_CONFIG, a dict, or configure()). Use `bank_preset()` for the
    ready-made bank-statement schema."""
    return Config(output_schema=[], synonyms={}, critical_fields=[])


def bank_preset() -> Config:
    """Ready-made preset for bank statements (Date, Narration, Reference, Debit,
    Credit, Balance) with debit/credit reconciliation. Also in config.example.json.

        from tabularmapper import bank_preset, configure
        configure(config=bank_preset())
    """
    return Config(
        output_schema=[FieldSpec(**d) for d in BANK_SCHEMA],
        synonyms={k: list(v) for k, v in BANK_SYNONYMS.items()},
        critical_fields=list(BANK_CRITICAL_FIELDS),
        reconcile=dict(BANK_RECONCILE),
        require_any=[list(g) for g in BANK_REQUIRE_ANY],
        row_keep_if_any=list(BANK_ROW_KEEP_IF_ANY),
        continuation_field=BANK_CONTINUATION_FIELD,
        extra_field_descriptions=dict(BANK_FIELD_DESCRIPTIONS),
    )


def config_from_dict(d: dict, _origin: str = "<dict>") -> Config:
    """Build a Config from a parsed JSON dict. This is the GENERIC path — nothing
    bank-specific is assumed; declare what you want."""
    if not d.get("output_schema"):
        _log.warning(
            "config %s has no non-empty 'output_schema' — nothing will be mapped. "
            "Provide output_schema (or use bank_preset() for the bank layout).",
            _origin)
    specs: list[FieldSpec] = []
    for item in d.get("output_schema") or []:
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
    # Synonyms are exactly what you declare — no bank defaults are merged in.
    syn = {k: list(v) for k, v in (d.get("synonyms") or {}).items()}
    crit = d.get("critical_fields") or []
    return Config(
        output_schema=specs,
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
      * None      -> env TABULARMAPPER_CONFIG, else the built-in defaults
      * dict      -> used directly
      * "s3://…"  -> S3 object (needs boto3) OR use a presigned https URL instead
      * "http(s)://…" / path / "file://…" -> fetched via stdlib urllib

    On any load/parse error, falls back to the defaults (so a bad or unreachable
    config never takes the service down) unless `strict=True`.
    """
    if source is None:
        source = os.getenv("TABULARMAPPER_CONFIG")
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
            "TABULARMAPPER config %r failed to load (%s: %s) — falling back to "
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
