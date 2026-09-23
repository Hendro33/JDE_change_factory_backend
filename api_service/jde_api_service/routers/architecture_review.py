"""
Architecture Review and Exact Change Approval (Gate 2).

Architecture Review normally starts on its own -- domain_governance.py's
application_manager_approve schedules it as a background task the
moment Gate 1 clears, the same way /changes/{id}/enhance already
starts Receive/Improve/Check. The manual trigger below exists only for
retry after a failed run; a human should not normally need it.

Gate 2 -- "Jade may execute this specific proposed change" -- calls
approval.py's approve_change()/reject_change(), passing the approver's
company and roles from the authenticated session.
This router does not create a second approval system: it resolves
which pending change record belongs to this story (via
approval.list_pending_changes(), the same read-only function
change_service.py already uses) and calls the real functions with it.
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from jde_mcp_server import approval, execution
from jde_mcp_server.ais_client import LiveReadUnavailable, client as ais
from jde_mcp_server.scope import ScopeViolation, load_company_scope, require_approval_policy

from ..config import settings
from ..dependencies import AuthContext, require_customer_access, require_write_access
from ..models.architecture_review import ArchitectureReviewRun, AskAboutSolutionInput
from ..models.change import PreflightResult, ReconcileTestInput, ReconcileWriteInput
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
            asked_by=ctx.identity.display_name,
            prior_turns=run.conversation,
            repo_root=settings.repo_root,
            customer_id=ctx.customer_id,
        )
    except ConversationError as exc:
        raise HTTPException(status_code=502, detail=f"could not get an answer: {exc}") from exc

    updated = run_service.append_conversation_turn(
        change_id,
        asked_by=ctx.identity.display_name,
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
    approval.approve_change() is the authoritative approval record, not
    a copy of it. It refuses unless the company has an approval policy
    and the caller holds a role that policy allows."""
    _require_queued_change(change_id, ctx.customer_id)
    record = _pending_change_record(change_id)
    try:
        # Authority comes from this company's approval policy and the
        # approver's roles on this company -- both from the session.
        approval.approve_change(
            record["change_id"],
            ctx.identity.display_name,
            company_id=ctx.customer_id,
            approver_roles=ctx.roles,
            approver_user_id=ctx.identity.id,
            note=payload.note,
        )
    except approval.ApproverNotAuthorised as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except (approval.ChangeApprovalError, ScopeViolation) as exc:
        # No policy, no scope, wrong company or not pending: refused, never defaulted.
        raise HTTPException(status_code=409, detail=str(exc))
    get_decision_feedback_service().record(
        change_id=change_id,
        customer_id=ctx.customer_id,
        kind="exact_change_approval",
        decided_by=ctx.identity.display_name,
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
    try:
        approval.reject_change(record["change_id"], ctx.identity.display_name, payload.note, company_id=ctx.customer_id)
    except approval.ChangeApprovalError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    get_decision_feedback_service().record(
        change_id=change_id,
        customer_id=ctx.customer_id,
        kind="exact_change_rejection",
        decided_by=ctx.identity.display_name,
        identity_id=ctx.identity.id,
        reason_code=payload.rejection_reason,
        note=payload.note,
    )
    change = get_change_service().get_for_customer(change_id, ctx.customer_id)
    assert change is not None
    return change.model_dump(mode="json", by_alias=True)


# ---------------------------------------------------------------------
# Execution state: preflight and reconciliation of unknown outcomes
# (mcp_server/jde_mcp_server/execution.py)
# ---------------------------------------------------------------------
def _change_record_for(story_id: str) -> dict:
    from ..services.change_service import _latest_change_record_for

    record = _latest_change_record_for(story_id)
    if record is None:
        raise HTTPException(status_code=409, detail=f"no exact change has been proposed for {story_id}")
    return record


def _require_policy_approver(ctx: AuthContext) -> None:
    """Reconciling decides whether a change may run again, so it needs the
    same authority as approving it: a role the company's policy allows."""
    try:
        policy = require_approval_policy(load_company_scope(ctx.customer_id))
    except ScopeViolation as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if not (set(ctx.roles) & set(policy["exact_change_approver_roles"])):
        raise HTTPException(status_code=403, detail="reconciling needs a role the company's approval policy allows")


@router.get("/changes/{change_id}/execution/preflight", response_model=PreflightResult)
def execution_preflight(change_id: str, ctx: AuthContext = Depends(require_customer_access)) -> PreflightResult:
    """What the execution gate would decide right now, check by check.
    Read-only: nothing is sent to JDE and no attempt is recorded."""
    _require_queued_change(change_id, ctx.customer_id)
    return PreflightResult.model_validate(approval.preflight(_change_record_for(change_id)["change_id"]))


@router.post("/changes/{change_id}/execution/reconcile")
def reconcile_write(
    change_id: str, payload: ReconcileWriteInput, ctx: AuthContext = Depends(require_write_access)
) -> dict:
    """Settle an unknown write outcome by checking the ACTUAL target value.
    Where Jade can read it (mock mode today), it reads it itself and any
    typed value is ignored; otherwise the person states the value they
    read in JDE, with a note, and that is recorded as human-verified."""
    _require_queued_change(change_id, ctx.customer_id)
    _require_policy_approver(ctx)
    record = _change_record_for(change_id)
    if record.get("company_id") != ctx.customer_id:
        raise HTTPException(status_code=404, detail=f"no such change: {change_id}")
    op = record["operation"]
    try:
        observed = ais.read_processing_option_value(op["application"], op["version"], op["option"])
        source = "automated read (mock JDE)"
        evidence_reference = (
            f"automated read of {op['application']}/{op['version']}/{op['option']} = {observed!r} "
            f"(mock JDE state)"
        )
    except LiveReadUnavailable as exc:
        if payload.observed_value is None or not payload.note.strip() or not payload.evidence_reference.strip():
            raise HTTPException(
                status_code=422, detail=f"{exc} Provide observedValue, a note and an evidenceReference."
            )
        observed, source, evidence_reference = payload.observed_value, "human-verified in JDE", payload.evidence_reference
    try:
        entry = execution.reconcile_write(
            record["change_id"], observed_value=observed, source=source,
            actor_user_id=ctx.identity.id, actor_name=ctx.identity.display_name,
            evidence_reference=evidence_reference, note=payload.note,
        )
    except execution.ExecutionBlocked as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except approval.ChangeApprovalError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {
        "outcome": entry["outcome"], "observedValue": observed, "source": source,
        "target": entry["target"], "evidenceReference": entry["evidence_reference"],
        "evidenceEntryHash": entry["evidence_entry_hash"],
    }


@router.post("/changes/{change_id}/execution/reconcile-test")
def reconcile_test(
    change_id: str, payload: ReconcileTestInput, ctx: AuthContext = Depends(require_write_access)
) -> dict:
    _require_queued_change(change_id, ctx.customer_id)
    _require_policy_approver(ctx)
    record = _change_record_for(change_id)
    if record.get("company_id") != ctx.customer_id:
        raise HTTPException(status_code=404, detail=f"no such change: {change_id}")
    try:
        entry = execution.reconcile_test(
            record["change_id"], ran=payload.ran, actor_user_id=ctx.identity.id,
            actor_name=ctx.identity.display_name, evidence_reference=payload.evidence_reference, note=payload.note,
        )
    except approval.ChangeApprovalError as exc:
        raise HTTPException(status_code=409 if isinstance(exc, execution.ExecutionBlocked) else 422, detail=str(exc))
    return {
        "outcome": entry["outcome"], "target": entry["target"], "evidenceReference": entry["evidence_reference"],
        "evidenceEntryHash": entry["evidence_entry_hash"],
    }

