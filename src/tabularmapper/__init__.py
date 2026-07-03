"""
tabularmapper — map any spreadsheet (.xlsx) to a schema you define.

Two-stage, auditable pipeline: deterministic header detection + synonym/fuzzy
column mapping, with an optional AI table matcher and a self-learning vocabulary.
The engine is domain-agnostic; "bank statements" is just a built-in preset.

Quick start:

    from tabularmapper import process_file, configure, config_from_dict
    configure(config_from_dict({"output_schema": [...], "synonyms": {...}}))
    res = process_file("file.xlsx")
    print(res.records)          # list[dict], ready for JSON / DB

    # or the ready-made bank layout:
    from tabularmapper import bank_preset, configure
    configure(config=bank_preset())

Heavier pieces are kept as submodules so importing this package stays light:
    from tabularmapper.ai_matcher import OpenAICompatibleMatcher
    from tabularmapper.api import router   # needs [api] extra
"""

from .engine import (
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
from .schema import (
    Config, bank_preset, config_from_dict, default_config, load_config,
)
from .stores import open_store

__version__ = "1.0.6"

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
    "bank_preset",
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
