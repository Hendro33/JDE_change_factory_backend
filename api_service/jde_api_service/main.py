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
from .routers import (
    admin,
    architecture_review,
    auth,
    change_requests,
    changes,
    company_users,
    domain_governance,
    session,
)
from .persistence.db import ensure_schema
from .services.bootstrap_service import ensure_bootstrap_admin
from .services.customer_service import ensure_seed_companies
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
    # records or resets anything already there.
    ensure_schema()
    ensure_seed_companies()
    ensure_bootstrap_admin()
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
    # Real login now: the session lives in an httponly cookie
    # (dependencies.py's resolve_identity), so the browser must be
    # allowed to send it cross-origin -- see config.py's own comment on
    # cookie_samesite for what a cross-origin deployment also needs.
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    # X-CSRF-Token: dependencies.verify_csrf_if_unsafe's double-submit
    # check -- the browser's CORS preflight refuses the actual request
    # outright if a custom header isn't declared here, which is exactly
    # what silently broke every state-changing call (including logout)
    # the moment the frontend started sending this header.
    allow_headers=["X-Customer-Id", "Content-Type", "X-CSRF-Token"],
)

app.include_router(auth.router)
app.include_router(session.router)
app.include_router(changes.router)
app.include_router(change_requests.router)
app.include_router(domain_governance.router)
app.include_router(architecture_review.router)
app.include_router(admin.router)
app.include_router(company_users.router)


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
