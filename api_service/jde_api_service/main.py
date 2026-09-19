"""
FastAPI application -- the API/application boundary for the JDE Change
Factory frontend. Sibling to mcp_server; imports it read-only and
never modifies its behaviour. Business logic lives in services/, not
in these route handlers -- see routers/.

Run locally:
    uvicorn jde_api_service.main:app --reload --port 8000
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .routers import change_requests, changes, session

app = FastAPI(
    title="JDE Change Factory API",
    description="Phase 1: read-only endpoints + direct-entry intake. "
    "See docs/ for the architecture analysis this implements.",
    version="0.1.0",
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


@app.get("/health", tags=["health"])
def health() -> dict:
    return {"status": "ok"}
