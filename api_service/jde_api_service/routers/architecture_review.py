"""
Architecture Review and Exact Change Approval (Gate 2).

Architecture Review normally starts on its own -- domain_governance.py's
application_manager_approve schedules it as a background task the
moment Gate 1 clears, the same way /changes/{id}/enhance already
starts Receive/Improve/Check. The manual trigger below exists only for
retry after a failed run; a human should not normally need it.

Gate 2 -- "Jade may execute this specific proposed change" -- reuses
approval.py's existing, unmodified approve_change()/reject_change().
This router does not create a second approval system: it resolves
which pending change record belongs to this story (via
approval.list_pending_changes(), the same read-only function
change_service.py already uses) and calls the real functions with it.
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from jde_mcp_server import approval

from ..config import settings
from ..dependencies import AuthContext, require_customer_access, require_write_access
from ..models.architecture_review import ArchitectureReviewRun, AskAboutSolutionInput
from ..models.domain_review import GovernanceDecisionInput
from ..services.architecture_driver import run_architecture_review
from ..services.conversation_driver import ConversationError, ask_about_solution
from ..services.registry import (
    get_architecture_review_service,
    get_change_service,
    get_decision_feedback_service,
    get_delivery_queue_service,
)

router = APIRouter(tags=["architecture-review"])


def _require_queued_change(change_id: str, customer_id: str):
    change = get_change_service().get_for_customer(change_id, customer_id)
    if change is None:
        raise HTTPException(status_code=404, detail=f"no such change: {change_id}")
    if get_delivery_queue_service().get(change_id) is None:
        raise HTTPException(
            status_code=409,
            detail=f"{change_id} is not in the Delivery Queue yet -- Architecture Review starts once "
            "the Application Manager has authorised it for delivery (Gate 1)",
        )
    return change


@router.get("/changes/{change_id}/architecture-review", response_model=ArchitectureReviewRun)
def get_architecture_review(
    change_id: str, ctx: AuthContext = Depends(require_customer_access)
) -> ArchitectureReviewRun:
    """Exposes the raw ArchitectureReviewRun -- including history (every
    completed analysis, never overwritten) and conversation ("Ask Jade
    about this solution" turns) -- to the frontend. Read-only; nothing
    here starts or changes a run."""
    _require_queued_change(change_id, ctx.customer_id)
    run = get_architecture_review_service().get(change_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"no architecture review yet for {change_id}")
    return run


@router.post("/changes/{change_id}/architecture-review/ask", response_model=ArchitectureReviewRun)
async def ask_about_solution_endpoint(
    change_id: str, payload: AskAboutSolutionInput, ctx: AuthContext = Depends(require_write_access)
) -> ArchitectureReviewRun:
    """"Ask Jade about this solution" -- Architect-backed, scoped to the
    solution already analysed for this story, never a general chatbot.
    Explanation never touches the analysis. A "recommend_reanalysis"
    turn is also never applied here -- there is no draft payload to
    apply, only a pointer back at the EXISTING manual retrigger
    (POST .../architecture-review) for an actual re-run, which appends
    its own new history entry rather than overwriting this one."""
    _require_queued_change(change_id, ctx.customer_id)
    run_service = get_architecture_review_service()
    run = run_service.get(change_id)
    if run is None or not run.history:
        raise HTTPException(status_code=409, detail="no completed architecture review to discuss yet for this change")
    if not payload.question.strip():
        raise HTTPException(status_code=422, detail="a question is required")

    try:
        result = await ask_about_solution(
            story_id=change_id,
            latest_version=run.history[-1],
            question=payload.question,
            asked_by=payload.asked_by,
            prior_turns=run.conversation,
            repo_root=settings.repo_root,
            customer_id=ctx.customer_id,
        )
    except ConversationError as exc:
        raise HTTPException(status_code=502, detail=f"could not get an answer: {exc}") from exc

    updated = run_service.append_conversation_turn(
        change_id,
        asked_by=payload.asked_by,
        question=payload.question,
        answer=result["answer"],
        kind=result["kind"],
        identity_id=ctx.identity.id,
    )
    assert updated is not None
    return updated


@router.post("/changes/{change_id}/architecture-review", status_code=202)
def start_architecture_review(
    change_id: str,
    background_tasks: BackgroundTasks,
    ctx: AuthContext = Depends(require_write_access),
) -> dict:
    """Manual (re)trigger -- normally unnecessary, since Gate 1 already
    starts this automatically. Useful if a run failed and needs a retry."""
    _require_queued_change(change_id, ctx.customer_id)
    run_service = get_architecture_review_service()
    existing = run_service.get(change_id)
    if existing is not None and existing.stage == "analyzing":
        raise HTTPException(status_code=409, detail=f"architecture review already in progress for {change_id}")

    background_tasks.add_task(
        run_architecture_review,
        story_id=change_id,
        repo_root=settings.repo_root,
        run_service=run_service,
        customer_id=ctx.customer_id,
    )
    return {"status": "started"}


def _pending_change_record(story_id: str) -> dict:
    pending = [c for c in approval.list_pending_changes() if c.get("story_id") == story_id]
    if not pending:
        raise HTTPException(
            status_code=409,
            detail=f"no exact change is currently pending approval for {story_id}",
        )
    # Same "most recent" tie-break change_service.py already uses.
    return max(pending, key=lambda c: c.get("created_at", 0))


@router.post("/changes/{change_id}/approve-change", status_code=200)
def approve_exact_change(
    change_id: str, payload: GovernanceDecisionInput, ctx: AuthContext = Depends(require_write_access)
) -> dict:
    """Gate 2 -- "Jade may execute this specific proposed change."
    Reuses approval.approve_change() unmodified; this is the
    authoritative approval record, not a copy of it."""
    _require_queued_change(change_id, ctx.customer_id)
    record = _pending_change_record(change_id)
    approval.approve_change(record["change_id"], payload.decided_by, note=payload.note)
    get_decision_feedback_service().record(
        change_id=change_id,
        customer_id=ctx.customer_id,
        kind="exact_change_approval",
        decided_by=payload.decided_by,
        identity_id=ctx.identity.id,
        note=payload.note,
    )
    change = get_change_service().get_for_customer(change_id, ctx.customer_id)
    assert change is not None
    return change.model_dump(mode="json", by_alias=True)


@router.post("/changes/{change_id}/reject-change", status_code=200)
def reject_exact_change(
    change_id: str, payload: GovernanceDecisionInput, ctx: AuthContext = Depends(require_write_access)
) -> dict:
    _require_queued_change(change_id, ctx.customer_id)
    if not payload.note:
        raise HTTPException(status_code=422, detail="a rejection must include a reason")
    record = _pending_change_record(change_id)
    approval.reject_change(record["change_id"], payload.decided_by, payload.note)
    get_decision_feedback_service().record(
        change_id=change_id,
        customer_id=ctx.customer_id,
        kind="exact_change_rejection",
        decided_by=payload.decided_by,
        identity_id=ctx.identity.id,
        reason_code=payload.rejection_reason,
        note=payload.note,
    )
    change = get_change_service().get_for_customer(change_id, ctx.customer_id)
    assert change is not None
    return change.model_dump(mode="json", by_alias=True)
