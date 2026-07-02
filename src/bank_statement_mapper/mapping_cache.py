"""
mapping_cache.py — persistent {header_fingerprint: field_mapping} cache.

A repeat bank format skips detection/mapping entirely -> true 100% on seen
formats. The fingerprint is a hash of the normalized header cell strings, so
the same header layout always resolves to the same cached mapping regardless
of row content.

Storage is pluggable via a URL (see stores.open_store):
    MappingCache()                              # env BANK_MAPPER_CACHE, else sqlite default
    MappingCache("sqlite:///mapping_cache.db")  # file, no server, concurrency-safe
    MappingCache("redis://localhost:6379/0")    # multi-worker
    MappingCache("memory://")                   # tests
    MappingCache(path="legacy.json")            # legacy JSON file (back-compat)
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Optional

from .bank_mapper import ColumnMap
from .stores import open_store

# SQLite by default — a file, no server to run, but concurrency-safe (unlike the
# old JSON file, which raced under multiple workers).
_DEFAULT_URL = "mapping_cache.db"


def _fingerprint(header: list, namespace: str = "") -> str:
    parts = []
    for c in header:
        s = "" if c is None else re.sub(r"\s+", " ", str(c).strip().lower())
        parts.append(s)
    # `namespace` scopes the key to the active schema, so a config change (e.g.
    # adding a field) does NOT return a stale mapping for the same header.
    raw = namespace + "\x00" + "|".join(parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


class MappingCache:
    def __init__(self, source: Optional[str] = None, *, path: Optional[str] = None):
        # precedence: explicit source > legacy path kwarg > env > sqlite default
        url = source or path or os.getenv("BANK_MAPPER_CACHE") or _DEFAULT_URL
        self.url = url
        self._store = open_store(url)

    def get(self, header: list, namespace: str = "") -> Optional[list[ColumnMap]]:
        entry = self._store.get(_fingerprint(header, namespace))
        if not entry:
            return None
        return [
            ColumnMap(m["col_index"], m["raw_header"], m["field"],
                      m["confidence"], "cache")
            for m in entry["columns"]
        ]

    def put(self, header: list, col_maps: list[ColumnMap],
            namespace: str = "") -> None:
        self._store.put(_fingerprint(header, namespace), {
            "header_preview": [("" if c is None else str(c)) for c in header],
            "columns": [
                {"col_index": m.col_index, "raw_header": m.raw_header,
                 "field": m.field, "confidence": m.confidence, "method": m.method}
                for m in col_maps
            ],
        })

    def close(self) -> None:
        self._store.close()
