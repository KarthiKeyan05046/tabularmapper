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

from . import bank_mapper                    # imported as a module so OUTPUT_SCHEMA is read
from .bank_mapper import process_stream  # dynamically (after configure), never a stale copy
from .mapping_cache import MappingCache


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
        field_defs=bank_mapper._ACTIVE_CONFIG.field_descriptions)


def build_learn_store():
    """Self-learning vocabulary store (URL via BANK_MAPPER_LEARN_STORE)."""
    from .learn import LearnStore
    return LearnStore()


class _State:
    cache: Optional[MappingCache] = None
    matcher: Any = None
    learn: Any = None


state = _State()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Load the output template + synonyms from BANK_MAPPER_CONFIG (file / URL /
    # s3:// / dict). Only if the env var is set — otherwise we keep whatever is
    # already active, so a manual `configure("config.json")` before startup is
    # NOT overwritten.
    _cfg = os.getenv("BANK_MAPPER_CONFIG")
    if _cfg:
        bank_mapper.configure(_cfg)
    state.cache = MappingCache()   # reads BANK_MAPPER_CACHE (URL) or the sqlite default
    state.matcher = build_matcher()
    state.learn = build_learn_store()
    bank_mapper.apply_learned(state.learn)   # activate already-learned synonyms
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
            learn_store=state.learn,
            source_label=file.filename or "<upload>",
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"could not process file: {exc}")

    return MapResponse(
        header_index=res.header_index,
        needs_review=res.needs_review,
        review_reasons=res.review_reasons,
        schema_columns=[disp for _, disp in bank_mapper.OUTPUT_SCHEMA],
        columns=[ColumnMapOut(**{
            "col_index": m.col_index, "raw_header": m.raw_header,
            "field": m.field, "confidence": m.confidence, "method": m.method,
        }) for m in res.column_maps],
        transactions=res.records,
    )


# --------------------------------------------------------------------------
# Learning review — approve/reject the gated (debit/credit) pending queue
# --------------------------------------------------------------------------
@router.get("/learn/pending")
async def learn_pending() -> dict:
    return {"pending": state.learn.pending(), "stats": state.learn.stats()}


@router.post("/learn/approve")
async def learn_approve(phrase: str, field: Optional[str] = None) -> dict:
    ok = await run_in_threadpool(state.learn.approve, phrase, field)
    if ok:
        bank_mapper.apply_learned(state.learn)   # activate immediately
    return {"approved": ok, "stats": state.learn.stats()}


@router.post("/learn/reject")
async def learn_reject(phrase: str, field: Optional[str] = None) -> dict:
    ok = await run_in_threadpool(state.learn.reject, phrase, field)
    return {"rejected": ok, "stats": state.learn.stats()}


# Standalone app (uvicorn bank_mapper_api:app)
app = FastAPI(title="Bank Statement Mapper", lifespan=lifespan)
app.include_router(router)
