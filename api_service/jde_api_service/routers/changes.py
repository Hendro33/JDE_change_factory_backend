from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from ..config import settings
from ..dependencies import AuthContext, require_customer_access, require_write_access
from ..models.change import Change
from ..models.metrics import ActivityEntry, FactoryMetrics
from ..models.work import MyWork
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
    ctx: AuthContext = Depends(require_write_access),
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

    from ..services import agent_settings

    try:
        agent_settings.require_enabled(ctx.customer_id, "receive-agent", "improve-agent", "check-agent")
    except agent_settings.AgentDisabled as exc:
        raise HTTPException(status_code=409, detail=str(exc))
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
        customer_id=ctx.customer_id,
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


@router.get("/work", response_model=MyWork)
def my_work(ctx: AuthContext = Depends(require_customer_access)) -> MyWork:
    """What needs the signed-in person's attention, derived only from the
    canonical lifecycle (services/lifecycle.py) and their roles here."""
    from datetime import datetime, timedelta, timezone

    from ..services.lifecycle import is_mine

    roles = set(ctx.roles)
    all_changes = [c for c in get_change_service().list_for_customer(ctx.customer_id) if c.lifecycle]
    open_ = [c for c in all_changes if c.lifecycle.phase != "done"]
    mine = [c for c in open_ if is_mine(c.lifecycle, roles)]
    mine_ids = {c.id for c in mine}
    # Decisions first, then tasks; oldest-waiting first within each.
    mine.sort(key=lambda c: (c.lifecycle.next_action.kind != "decision", c.updated_at))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    return MyWork(
        needs_you=mine,
        waiting_on_others=[c for c in open_ if c.id not in mine_ids
                           and c.lifecycle.next_action.owner not in ("jade", "none")],
        jade_working=[c for c in open_ if c.lifecycle.next_action.owner == "jade"],
        in_progress_count=len(open_),
        completed_this_month=[c for c in all_changes if c.lifecycle.phase == "done"
                              and c.lifecycle.outcome != "rejected" and c.updated_at >= cutoff],
        roles=sorted(roles),
    )
