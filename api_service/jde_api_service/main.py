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
import os
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
from .persistence.db import db_path, ensure_schema
from .persistence.revisions import RevisionConflict, RevisionRequired
from jde_mcp_server import scope as mcp_scope

from .services.bootstrap_service import ensure_bootstrap_admin
from .services.run_recovery import reconcile_interrupted_runs
from .services.customer_service import ensure_seed_companies
from .services.registry import (
    get_business_domain_service,
    get_change_request_service,
    get_customer_link_service,
    get_jira_credentials_service,
)
from .services.seed_service import (
    ensure_bicycleworks_business_domains,
    ensure_bicycleworks_pilot_dataset,
    ensure_t001_backlog_link,
)


@asynccontextmanager
def _wire_execution_gate() -> None:
    """Point mcp_server's execution gate at the records this service
    owns: each company's Admin-saved engagement scope and the story ->
    company links recorded at intake. Exported as environment variables
    too, so an MCP server process started for an agent run inherits the
    same records. An explicit environment setting wins."""
    wiring = {
        "JDE_COMPANY_SCOPE_DIR": os.path.join(settings.data_dir, "engagement_scope"),
        "JDE_STORY_COMPANY_DIR": os.path.join(settings.data_dir, "customer_links"),
        # Read-only: the gate re-checks the approver's CURRENT roles here
        # immediately before dispatch (mcp_server authority.py).
        "JDE_AUTH_DB_PATH": db_path(),
    }
    for name, default in wiring.items():
        os.environ.setdefault(name, os.path.abspath(default))
    mcp_scope.COMPANY_SCOPE_DIR = os.environ["JDE_COMPANY_SCOPE_DIR"]
    mcp_scope.STORY_COMPANY_DIR = os.environ["JDE_STORY_COMPANY_DIR"]


async def _lifespan(app: FastAPI):
    # Idempotent -- safe on every restart, never duplicates existing
    # records or resets anything already there.
    _wire_execution_gate()
    interrupted = reconcile_interrupted_runs()
    if any(interrupted.values()):
        logger.warning("Marked runs interrupted by the restart as failed: %s", interrupted)
    ensure_schema()
    reencrypted = get_jira_credentials_service().reencrypt_stored()
    if reencrypted:
        logger.info("Encrypted or re-keyed %d stored Jira credential(s)", reencrypted)
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


@app.exception_handler(RevisionConflict)
async def _revision_conflict_handler(request: Request, exc: RevisionConflict) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc), "currentRevision": exc.current_revision})


@app.exception_handler(RevisionRequired)
async def _revision_required_handler(request: Request, exc: RevisionRequired) -> JSONResponse:
    return JSONResponse(status_code=428, content={"detail": str(exc), "currentRevision": exc.current_revision})


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
