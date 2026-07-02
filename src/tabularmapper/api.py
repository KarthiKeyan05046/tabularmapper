"""
api.py — drop-in FastAPI router for tabularmapper.

Two ways to use it from your existing backend:

  A) Mount the router on your app (prefix defaults to /mapper):

        from fastapi import FastAPI
        from tabularmapper.api import router, lifespan
        app = FastAPI(lifespan=lifespan)      # builds cache + matcher once
        app.include_router(router)
        # -> POST /mapper/map , GET /mapper/health

     Custom prefix: `make_router("/catalog")`, or set TABULARMAPPER_ROUTE_PREFIX.

  B) Run it standalone:

        uvicorn tabularmapper.api:app --reload

Design notes:
  * The MappingCache and the (optional) AI matcher are built ONCE in `lifespan`
    and reused across requests — not per call.
  * `process_file` is synchronous (openpyxl + a possible blocking LLM HTTP call),
    so it runs in a threadpool to avoid blocking the event loop.
  * If OPENAI_API_KEY is unset, the AI matcher is simply off: known banks still
    map deterministically; unknown ones come back with needs_review=True.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from . import engine                    # imported as a module so OUTPUT_SCHEMA is read
from .engine import OutputResult, process_stream  # dynamically (after configure), never a stale copy
from .mapping_cache import MappingCache

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _default_threshold() -> int:
    """The fuzzy-accept gate (0-100). Below this, a column is left unmapped and,
    if it's a critical field, the AI matcher is asked to fill it. Raise it to
    push borderline fuzzy matches to the AI instead of trusting them. Read from
    TABULARMAPPER_THRESHOLD at request time; falls back to 80."""
    try:
        return max(0, min(100, int(os.getenv("TABULARMAPPER_THRESHOLD", "80"))))
    except (TypeError, ValueError):
        return 80


class OutFormat(str, Enum):
    """Response shape for POST /map — rendered as a dropdown in the docs."""
    json = "json"        # rows inline (default)
    base64 = "base64"    # rows inline + a mapped .xlsx in file_base64
    file = "file"        # download the .xlsx directly (binary, no JSON body)


# --------------------------------------------------------------------------
# Shared singletons (built once at startup)
# --------------------------------------------------------------------------
def build_matcher():
    """Return an OpenAICompatibleMatcher if OPENAI_API_KEY is set, else None
    (deterministic-only mode)."""
    if not os.getenv("OPENAI_API_KEY"):
        return None
    from .ai_matcher import OpenAICompatibleMatcher
    # field descriptions come from the active config (not hardcoded)
    return OpenAICompatibleMatcher(
        field_defs=engine._ACTIVE_CONFIG.field_descriptions)


def build_learn_store():
    """Self-learning vocabulary store (URL via TABULARMAPPER_LEARN_STORE)."""
    from .learn import LearnStore
    return LearnStore()


class _State:
    cache: Optional[MappingCache] = None
    matcher: Any = None
    learn: Any = None


state = _State()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Load the output template + synonyms from TABULARMAPPER_CONFIG (file / URL /
    # s3:// / dict). Only if the env var is set — otherwise we keep whatever is
    # already active, so a manual `configure("config.json")` before startup is
    # NOT overwritten.
    _cfg = os.getenv("TABULARMAPPER_CONFIG")
    if _cfg:
        engine.configure(_cfg)
    state.cache = MappingCache()   # reads TABULARMAPPER_CACHE (URL) or the sqlite default
    state.matcher = build_matcher()
    state.learn = build_learn_store()
    engine.apply_learned(state.learn)   # activate already-learned synonyms
    yield
    # nothing to tear down


# --------------------------------------------------------------------------
# Response schema
# --------------------------------------------------------------------------
class ColumnMapOut(BaseModel):
    col_index: int
    raw_header: str
    field: Optional[str]
    confidence: int
    method: str


class MapResponse(BaseModel):
    header_index: int
    needs_review: bool
    review_reasons: list[str]
    schema_columns: list[str]
    columns: list[ColumnMapOut]
    transactions: list[dict]
    # Populated only when ?format=base64 — a base64-encoded .xlsx of the mapped
    # rows, ready to decode and save client-side. None otherwise.
    file_base64: Optional[str] = None


# --------------------------------------------------------------------------
# Endpoint handlers (plain functions so the router prefix can be configured)
# --------------------------------------------------------------------------
async def health() -> dict:
    return {"status": "ok", "ai_enabled": state.matcher is not None}


_WEB_DIR = Path(__file__).resolve().parent / "web"


async def config_page() -> HTMLResponse:
    """Serve the self-contained config-builder page (src/tabularmapper/web/index.html)."""
    index = _WEB_DIR / "index.html"
    if not index.is_file():
        raise HTTPException(status_code=404, detail="config builder page not found")
    return HTMLResponse(index.read_text(encoding="utf-8"))


async def map_statement(
    file: UploadFile = File(...),
    format: OutFormat = Query(
        OutFormat.json,
        description="json = rows inline (default); base64 = rows inline + an "
                    ".xlsx encoded in file_base64; file = download the .xlsx "
                    "directly (binary, no JSON body).",
    ),
    threshold: Optional[int] = Query(
        None,
        ge=0, le=100,
        description="Fuzzy-accept gate 0-100. Overrides TABULARMAPPER_THRESHOLD "
                    "(default 80) for this request. Raise it to send borderline "
                    "fuzzy matches to the AI matcher instead of trusting them.",
    ),
):
    """Upload a spreadsheet (.xlsx); get the standardized mapping + rows.

    `format` controls what comes back:
      * json    -> MapResponse with the rows in `transactions`
      * base64  -> same MapResponse, plus a mapped .xlsx in `file_base64`
      * file    -> the mapped .xlsx as a downloadable attachment

    `threshold` (query) overrides the fuzzy gate for this one call; otherwise the
    server default (TABULARMAPPER_THRESHOLD, else 80) is used.
    """
    name = (file.filename or "").lower()
    if not name.endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="expected an .xlsx/.xls file")

    gate = threshold if threshold is not None else _default_threshold()
    data = await file.read()          # raw bytes, parsed in memory (never hits disk)
    try:
        # blocking work -> threadpool; process_stream reads straight from bytes
        res = await run_in_threadpool(
            process_stream, data,
            table_matcher=state.matcher, cache=state.cache,
            learn_store=state.learn, threshold=gate,
            source_label=file.filename or "<upload>",
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"could not process file: {exc}")

    # file -> stream the mapped .xlsx straight back as a download (no JSON body).
    if format == "file":
        xlsx = await run_in_threadpool(
            lambda: OutputResult(records=res.records, format="bytes").bytes)
        stem = os.path.splitext(os.path.basename(file.filename or "mapped"))[0]
        return Response(
            content=xlsx,
            media_type=_XLSX_MIME,
            headers={"Content-Disposition": f'attachment; filename="{stem}_mapped.xlsx"'},
        )

    file_b64 = None
    if format == "base64":
        file_b64 = await run_in_threadpool(
            lambda: OutputResult(records=res.records, format="base64").base64)

    return MapResponse(
        header_index=res.header_index,
        needs_review=res.needs_review,
        review_reasons=res.review_reasons,
        schema_columns=[disp for _, disp in engine.OUTPUT_SCHEMA],
        columns=[ColumnMapOut(**{
            "col_index": m.col_index, "raw_header": m.raw_header,
            "field": m.field, "confidence": m.confidence, "method": m.method,
        }) for m in res.column_maps],
        transactions=res.records,
        file_base64=file_b64,
    )


async def learn_pending() -> dict:
    return {"pending": state.learn.pending(), "stats": state.learn.stats()}


async def learn_approve(phrase: str, field: Optional[str] = None) -> dict:
    ok = await run_in_threadpool(state.learn.approve, phrase, field)
    if ok:
        engine.apply_learned(state.learn)   # activate immediately
    return {"approved": ok, "stats": state.learn.stats()}


async def learn_reject(phrase: str, field: Optional[str] = None) -> dict:
    ok = await run_in_threadpool(state.learn.reject, phrase, field)
    return {"rejected": ok, "stats": state.learn.stats()}


# --------------------------------------------------------------------------
# Router factory — the prefix is configurable (default "/mapper", or the env
# var TABULARMAPPER_ROUTE_PREFIX). This is a general table->schema mapper, so the
# route name isn't bank-specific and you can set your own.
# --------------------------------------------------------------------------
def make_router(prefix: Optional[str] = None, tags: Optional[list] = None) -> APIRouter:
    if prefix is None:
        prefix = os.getenv("TABULARMAPPER_ROUTE_PREFIX", "/mapper")
    r = APIRouter(prefix=prefix.rstrip("/"), tags=tags or ["mapper"])
    r.add_api_route("/health", health, methods=["GET"])
    r.add_api_route("/config", config_page, methods=["GET"],
                    response_class=HTMLResponse, include_in_schema=False)
    r.add_api_route("/map", map_statement, methods=["POST"], response_model=MapResponse)
    r.add_api_route("/learn/pending", learn_pending, methods=["GET"])
    r.add_api_route("/learn/approve", learn_approve, methods=["POST"])
    r.add_api_route("/learn/reject", learn_reject, methods=["POST"])
    return r


# Default router instance -> /mapper/*  (or TABULARMAPPER_ROUTE_PREFIX)
router = make_router()

# Standalone app (uvicorn tabularmapper.api:app)
app = FastAPI(title="Tabular Mapper", lifespan=lifespan)
app.include_router(router)
