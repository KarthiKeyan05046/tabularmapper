"""
bank_statement_mapper — map any bank statement .xlsx to a standard schema.

Two-stage, auditable pipeline: deterministic header detection + synonym/fuzzy
column mapping, with an optional AI table matcher and a self-learning vocabulary.

Quick start:

    from bank_statement_mapper import process_file, MappingCache
    res = process_file("statement.xlsx", cache=MappingCache())
    print(res.records)          # list[dict], ready for JSON / DB

Heavier pieces are kept as submodules so importing this package stays light:
    from bank_statement_mapper.ai_matcher import OpenAICompatibleMatcher
    from bank_statement_mapper.bank_mapper_api import router   # needs [api] extra
"""

from .bank_mapper import (
    ALLOWED_FIELDS,
    OUTPUT_SCHEMA,
    ColumnMap,
    OutputResult,
    ProcessResult,
    apply_learned,
    configure,
    detect_header_row,
    map_columns,
    normalize_amount,
    normalize_date,
    process_file,
    process_stream,
    records_to_csv_bytes,
)
from .learn import LearnStore, harvest_folder, learn_from_result
from .mapping_cache import MappingCache
from .schema import Config, config_from_dict, default_config, load_config
from .stores import open_store

__version__ = "0.1.0"

__all__ = [
    "process_file",
    "process_stream",
    "records_to_csv_bytes",
    "configure",
    "apply_learned",
    "MappingCache",
    "LearnStore",
    "learn_from_result",
    "harvest_folder",
    "load_config",
    "config_from_dict",
    "default_config",
    "Config",
    "open_store",
    "ProcessResult",
    "ColumnMap",
    "OutputResult",
    "OUTPUT_SCHEMA",
    "ALLOWED_FIELDS",
    "detect_header_row",
    "map_columns",
    "normalize_amount",
    "normalize_date",
    "__version__",
]
