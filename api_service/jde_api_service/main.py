"""
FastAPI application -- the API/application boundary for the JDE Change
Factory frontend. Sibling to mcp_server; imports it read-only and
never modifies its behaviour. Business logic lives in services/, not
in these route handlers -- see routers/.

Run locally:
    uvicorn jde_api_service.main:app --reload --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import settings

logger = logging.getLogger("jde_api_service")
from .routers import change_requests, changes, domain_governance, session
from .services.registry import (
    get_business_domain_service,
    get_change_request_service,
    get_customer_link_service,
)
from .services.seed_service import (
    ensure_bicycleworks_business_domains,
    ensure_bicycleworks_pilot_dataset,
    ensure_t001_backlog_link,
)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Idempotent -- safe on every restart, never duplicates existing
    # records. See services/seed_service.py.
    ensure_bicycleworks_pilot_dataset(get_change_request_service())
    ensure_bicycleworks_business_domains(get_business_domain_service())
    ensure_t001_backlog_link(get_customer_link_service())
    yield


app = FastAPI(
    title="Jade API",
    description="Phase 1: read-only endpoints + direct-entry intake. "
    "See docs/ for the architecture analysis this implements.",
    version="0.1.0",
    lifespan=_lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["X-Customer-Id", "X-Demo-User-Id", "Content-Type"],
)

app.include_router(session.router)
app.include_router(changes.router)
app.include_router(change_requests.router)
app.include_router(domain_governance.router)


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # FastAPI's exception handlers run INSIDE the CORS middleware, so
    # the response below carries CORS headers -- Starlette's own
    # default 500 (for anything with no registered handler) does not,
    # which the browser then reports as a CORS failure rather than the
    # real server error it's masking.
    logger.exception("unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "internal server error"})


@app.get("/health", tags=["health"])
def health() -> dict:
    return {"status": "ok"}
