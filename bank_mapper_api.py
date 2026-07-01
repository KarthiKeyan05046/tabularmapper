"""
bank_mapper_api.py — drop-in FastAPI router for the bank statement mapper.

Two ways to use it from your existing backend:

  A) Mount the router on your app:

        from fastapi import FastAPI
        from bank_mapper_api import router, lifespan
        app = FastAPI(lifespan=lifespan)      # builds cache + matcher once
        app.include_router(router)
        # -> POST /statements/map , GET /statements/health

  B) Run it standalone:

        uvicorn bank_mapper_api:app --reload

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
from typing import Any, Optional

from fastapi import APIRouter, FastAPI, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from bank_mapper import OUTPUT_SCHEMA, process_stream
from mapping_cache import MappingCache


# --------------------------------------------------------------------------
# Shared singletons (built once at startup)
# --------------------------------------------------------------------------
def build_matcher():
    """Return an OpenAICompatibleMatcher if OPENAI_API_KEY is set, else None
    (deterministic-only mode)."""
    if not os.getenv("OPENAI_API_KEY"):
        return None
    from ai_matcher import OpenAICompatibleMatcher
    return OpenAICompatibleMatcher()  # reads OPENAI_BASE_URL / OPENAI_MODEL too


class _State:
    cache: Optional[MappingCache] = None
    matcher: Any = None


state = _State()


@asynccontextmanager
async def lifespan(app: FastAPI):
    state.cache = MappingCache(os.getenv("BANK_MAPPER_CACHE", "mapping_cache.json"))
    state.matcher = build_matcher()
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


# --------------------------------------------------------------------------
# Router
# --------------------------------------------------------------------------
router = APIRouter(prefix="/statements", tags=["statements"])


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "ai_enabled": state.matcher is not None}


@router.post("/map", response_model=MapResponse)
async def map_statement(file: UploadFile = File(...)) -> MapResponse:
    """Upload a bank statement .xlsx; get the standardized mapping + rows."""
    name = (file.filename or "").lower()
    if not name.endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="expected an .xlsx/.xls file")

    data = await file.read()          # raw bytes, parsed in memory (never hits disk)
    try:
        # blocking work -> threadpool; process_stream reads straight from bytes
        res = await run_in_threadpool(
            process_stream, data,
            table_matcher=state.matcher, cache=state.cache,
            source_label=file.filename or "<upload>",
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"could not process file: {exc}")

    return MapResponse(
        header_index=res.header_index,
        needs_review=res.needs_review,
        review_reasons=res.review_reasons,
        schema_columns=[disp for _, disp in OUTPUT_SCHEMA],
        columns=[ColumnMapOut(**{
            "col_index": m.col_index, "raw_header": m.raw_header,
            "field": m.field, "confidence": m.confidence, "method": m.method,
        }) for m in res.column_maps],
        transactions=res.records,
    )


# Standalone app (uvicorn bank_mapper_api:app)
app = FastAPI(title="Bank Statement Mapper", lifespan=lifespan)
app.include_router(router)
