from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..dependencies import AuthContext, require_customer_access
from ..models.change import Change
from ..models.metrics import ActivityEntry, FactoryMetrics
from ..services.registry import get_change_service, get_metrics_service

router = APIRouter(tags=["changes"])


@router.get("/changes", response_model=list[Change])
def list_changes(ctx: AuthContext = Depends(require_customer_access)) -> list[Change]:
    return get_change_service().list_for_customer(ctx.customer_id)


@router.get("/changes/{change_id}", response_model=Change)
def get_change(change_id: str, ctx: AuthContext = Depends(require_customer_access)) -> Change:
    change = get_change_service().get_for_customer(change_id, ctx.customer_id)
    if change is None:
        # Deliberately 404, not 403: a change that exists for a
        # different customer must look identical to one that doesn't
        # exist at all -- confirming existence is itself a leak.
        raise HTTPException(status_code=404, detail=f"no such change: {change_id}")
    return change


@router.get("/backlog", response_model=list[Change])
def get_backlog(ctx: AuthContext = Depends(require_customer_access)) -> list[Change]:
    return get_change_service().backlog_for_customer(ctx.customer_id)


@router.get("/metrics", response_model=FactoryMetrics)
def get_metrics(ctx: AuthContext = Depends(require_customer_access)) -> FactoryMetrics:
    return get_metrics_service().metrics_for_customer(ctx.customer_id)


@router.get("/activity", response_model=list[ActivityEntry])
def get_activity(ctx: AuthContext = Depends(require_customer_access)) -> list[ActivityEntry]:
    return get_metrics_service().activity_for_customer(ctx.customer_id)
