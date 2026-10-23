from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from ..config import settings
from ..dependencies import AuthContext, require_customer_access, require_write_access
from ..models.base import ApiModel
from ..models.change import Change, ChangeType, Priority
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


class ClassificationInput(ApiModel):
    priority: Optional[Priority] = None
    change_type: Optional[ChangeType] = None
    # The classificationRevision the caller loaded (0 when none was set yet).
    expected_revision: int


@router.put("/changes/{change_id}/classification", response_model=Change)
def set_classification(change_id: str, payload: ClassificationInput,
                       ctx: AuthContext = Depends(require_write_access)) -> Change:
    """Sets the story's priority and/or change type. Any member who may
    write (not a viewer) can set them; the history records who and when.
    Neither value takes part in any approval or lifecycle decision."""
    from ..services import classification_service

    if get_change_service().get_for_customer(change_id, ctx.customer_id) is None:
        raise HTTPException(status_code=404, detail=f"no such change: {change_id}")
    try:
        classification_service.set_classification(
            change_id, ctx.customer_id, priority=payload.priority, change_type=payload.change_type,
            actor_id=ctx.identity.id, actor=ctx.identity.display_name, expected_revision=payload.expected_revision)
    except classification_service.ClassificationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return get_change_service().get_for_customer(change_id, ctx.customer_id)


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
def get_metrics(ctx: AuthContext = Depends(require_customer_access),
                period: Literal["week", "month", "year", "lifetime"] = "lifetime") -> FactoryMetrics:
    return get_metrics_service().metrics_for_customer(ctx.customer_id, period=period)


@router.get("/activity", response_model=list[ActivityEntry])
def get_activity(ctx: AuthContext = Depends(require_customer_access)) -> list[ActivityEntry]:
    return get_metrics_service().activity_for_customer(ctx.customer_id)


@router.get("/work", response_model=MyWork)
def my_work(ctx: AuthContext = Depends(require_customer_access)) -> MyWork:
    """What needs the signed-in person's attention, derived only from the
    canonical lifecycle and their roles (and, for a Domain Owner, their
    domains) here. Sections and figures come from services/work.py."""
    from ..services import membership_service, work
    from ..services.registry import get_business_domain_service

    changes = [c for c in get_change_service().list_for_customer(ctx.customer_id) if c.lifecycle]
    domains = membership_service.domain_ids_for_membership(ctx.identity.id, ctx.customer_id)
    try:
        names = {d.id: d.name for d in get_business_domain_service().list_for_customer(ctx.customer_id)}
    except Exception:  # noqa: BLE001 -- names are presentation only
        names = {}
    return work.build(changes, ctx.roles, domains, names)


@router.get("/figures")
def portfolio_figures(ctx: AuthContext = Depends(require_customer_access)) -> list[dict]:
    """The portfolio figures of the one figure strip (decision X1), the same
    numbers My Work and the Application Manager home show."""
    from ..services import work

    changes = [c for c in get_change_service().list_for_customer(ctx.customer_id) if c.lifecycle]
    return [f.model_dump(by_alias=True) for f in work.figures(changes)]


# -- Owned issues ----------------------------------------------------------------------------
class ResolveIssueInput(ApiModel):
    # "answered" records an answer to a question; "resolved" says the point is
    # dealt with (for example a setup gap fixed in Administration);
    # "reopened" withdraws an earlier answer or resolution.
    status: Literal["answered", "resolved", "reopened"] = "answered"
    answer: str = ""


@router.post("/changes/{change_id}/issues/{issue_id}/resolution", response_model=Change)
def resolve_issue(change_id: str, issue_id: str, payload: ResolveIssueInput,
                  ctx: AuthContext = Depends(require_write_access)) -> Change:
    """Record an answer or a resolution on one of the story's open points.
    Recording it never changes the story or the design by itself: the next
    Architect run is given the answers and reassesses. Who may record it:
    the issue's owner (a Domain Owner only for a story in their domain), the
    Application Manager, or an Administrator."""
    from ..dependencies import require_domain_owner_access
    from ..services import issues as issues_service

    change = get_change_service().get_for_customer(change_id, ctx.customer_id)
    if change is None or change.lifecycle is None:
        raise HTTPException(status_code=404, detail="This story could not be found.")
    issue = next((i for i in change.lifecycle.issues if i.id == issue_id), None)
    if issue is None:
        raise HTTPException(status_code=404, detail="This open point is no longer on the story (it may have been "
                                                    "replaced by a newer design).")
    if issue.kind == "jade":
        raise HTTPException(status_code=409, detail="JADE resolves this point itself; nothing needs to be recorded.")
    if issue.source == "delivery_gate" and payload.status != "reopened":
        raise HTTPException(status_code=409, detail="This point comes from the delivery check itself. It clears "
                                                    "when its cause is fixed; it cannot be marked resolved by hand.")
    if payload.status == "answered" and not payload.answer.strip():
        raise HTTPException(status_code=422, detail="Write the answer first.")
    roles = set(ctx.roles)
    if not roles & {"product_manager", "admin"}:
        if issue.owner == "domain_owner" and "domain_owner" in roles:
            require_domain_owner_access(ctx, change.business_domain_id)
        elif issue.owner not in roles:
            raise HTTPException(status_code=403, detail=f"Only the {issue.owner_label or 'owner'}, the Application "
                                                        "Manager or an Administrator can record this.")
    issues_service.record_resolution(ctx.customer_id, change_id, issue=issue, status=payload.status,
                                     answer=payload.answer, by=ctx.identity.display_name,
                                     by_user_id=ctx.identity.id)
    return get_change_service().get_for_customer(change_id, ctx.customer_id)


@router.get("/completion-gaps")
def completion_gaps(ctx: AuthContext = Depends(require_customer_access)) -> list[dict]:
    """Finished stories whose record lacks a checkpoint the current workflow
    requires (audit 23 Oct): for the Application Manager to review. Nothing is
    changed by listing them."""
    if not set(ctx.roles) & {"admin", "product_manager"}:
        raise HTTPException(status_code=403, detail="This list is for Administrators and Application Managers.")
    return [{"id": c.id, "title": c.title, "outcome": c.lifecycle.outcome, "gaps": c.lifecycle.completion_gaps}
            for c in get_change_service().list_for_customer(ctx.customer_id)
            if c.lifecycle and c.lifecycle.completion_gaps]


@router.get("/setup-list")
def setup_list(ctx: AuthContext = Depends(require_customer_access)) -> list[dict]:
    """The Administrator's setup list: one task per setup gap, with every
    story it affects (Administrators and Application Managers)."""
    from ..services import issues as issues_service

    if not set(ctx.roles) & {"admin", "product_manager"}:
        raise HTTPException(status_code=403, detail="The setup list is for Administrators and Application Managers.")
    open_ = [c for c in get_change_service().list_for_customer(ctx.customer_id)
             if c.lifecycle and c.lifecycle.phase != "done"]
    return issues_service.setup_list(open_)
