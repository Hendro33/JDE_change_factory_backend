from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from ..config import settings
from ..dependencies import AuthContext, require_customer_access
from ..models.change import Change
from ..models.metrics import ActivityEntry, FactoryMetrics
from ..services.orchestration_driver import run_enhancement
from ..services.registry import (
    get_change_request_service,
    get_change_service,
    get_customer_link_service,
    get_enhancement_run_service,
    get_metrics_service,
)

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


@router.post("/changes/{change_id}/enhance", response_model=Change, status_code=202)
async def enhance_change(
    change_id: str,
    background_tasks: BackgroundTasks,
    ctx: AuthContext = Depends(require_customer_access),
) -> Change:
    """Starts Receive -> Improve -> Check (orchestration_driver.py)
    against an existing, not-yet-promoted ChangeRequest, as a
    background task -- a real run can take minutes, so this returns
    immediately (202) rather than blocking the request. Poll
    GET /changes/{id} for progress via its processingStage field."""
    change_request_service = get_change_request_service()
    cr = change_request_service.get(change_id)
    if cr is None or cr.customer_id != ctx.customer_id:
        raise HTTPException(status_code=404, detail=f"no such change request: {change_id}")

    run_service = get_enhancement_run_service()
    existing = run_service.get(change_id)
    if existing is not None and existing.stage in ("receiving", "improving", "checking"):
        raise HTTPException(status_code=409, detail=f"enhancement already in progress for {change_id}")

    source = ", ".join(p for p in (cr.business_source, cr.source_reference) if p)
    background_tasks.add_task(
        run_enhancement,
        request_id=change_id,
        story_id=change_id,
        source=source,
        raw_content=cr.raw_content,
        repo_root=settings.repo_root,
        run_service=run_service,
        link_service=get_customer_link_service(),
    )

    change = get_change_service().get_for_customer(change_id, ctx.customer_id)
    assert change is not None  # just confirmed cr exists above
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
