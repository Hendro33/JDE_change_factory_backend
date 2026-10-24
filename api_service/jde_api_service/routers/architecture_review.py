"""
Architecture Review and Exact Change Approval (Gate 2).

Architecture Review normally starts on its own -- domain_governance.py's
application_manager_approve schedules it as a background task the
moment Gate 1 clears, the same way /changes/{id}/enhance already
starts Receive/Improve/Check. The manual trigger below exists only for
retry after a failed run; a human should not normally need it.

Gate 2 -- "this specific proposed change may be delivered" -- calls
approval.py's approve_change()/reject_change(), passing the approver's
company and roles from the authenticated session. After approval the agents
apply the change set's agent items in DEV (executors/runner.py); a person
applies and records only the items marked for a person
(delivery/functional.py), and Jade verifies every item live.
This router does not create a second approval system: it resolves
which pending change record belongs to this story (via
approval.list_pending_changes(), the same read-only function
change_service.py already uses) and calls the real functions with it.
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from jde_mcp_server import approval, authority, execution
from jde_mcp_server.binding import BindingInvalid
from jde_mcp_server.capability_catalog import CapabilityError
from jde_mcp_server.scope import ScopeViolation, load_company_scope, require_approval_policy

from ..config import settings
from ..dependencies import AuthContext, require_customer_access, require_write_access
from ..models.architecture_review import ArchitectureReviewRun, AskAboutSolutionInput
from ..delivery import functional as delivery
from ..models.change import (
    PreflightResult,
    ReconcileTestInput,
    ReconcileWriteInput,
    RecordAppliedInput,
    RecordTestResultInput,
)
from ..models.base import ApiModel
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

    from ..services import agent_settings

    try:
        agent_settings.require_enabled(ctx.customer_id, "architect")
    except agent_settings.AgentDisabled as exc:
        raise HTTPException(status_code=409, detail=str(exc))
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
        proposed_question=result.get("proposed_question"),
    )
    assert updated is not None
    return updated


@router.post("/changes/{change_id}/architecture-review/conversation/{turn_id}/apply", response_model=ArchitectureReviewRun)
def apply_solution_proposal(change_id: str, turn_id: str, ctx: AuthContext = Depends(require_write_access)) -> ArchitectureReviewRun:
    """Register C11: a person applies what the conversation proposed. A
    proposed question becomes an owned question for the business on the story,
    recorded with who applied it; nothing else changes."""
    from ..services import issues as issues_service

    _require_queued_change(change_id, ctx.customer_id)
    if not (ctx.roles & {"product_manager", "admin"}):
        raise HTTPException(status_code=403, detail="the Application Manager applies a proposal from the conversation")
    run_service = get_architecture_review_service()
    run = run_service.get(change_id)
    turn = next((t for t in (run.conversation if run else []) if t.turn_id == turn_id), None)
    if turn is None or turn.kind != "propose_question" or not turn.proposed_question:
        raise HTTPException(status_code=404, detail="no proposal to apply on that turn")
    if turn.applied_at:
        raise HTTPException(status_code=409, detail="this proposal was already applied")
    issues_service.add_conversation_question(ctx.customer_id, change_id, question=turn.proposed_question,
                                             by=ctx.identity.display_name, turn_id=turn_id)
    updated = run_service.mark_turn_applied(change_id, turn_id, by=ctx.identity.display_name)
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
    from ..services import agent_settings

    try:
        agent_settings.require_enabled(ctx.customer_id, "architect")
    except agent_settings.AgentDisabled as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    # Marked as running before answering, so every screen shows "at work"
    # at once and a second click is refused (409) instead of starting a
    # second run. The run itself calls start() again, which keeps history.
    run_service.start(change_id)
    background_tasks.add_task(
        run_architecture_review,
        story_id=change_id,
        repo_root=settings.repo_root,
        run_service=run_service,
        customer_id=ctx.customer_id,
        initiated_by=ctx.identity.id,
    )
    return {"status": "started"}


def _pending_change_record(story_id: str) -> dict:
    pending = [c for c in approval.list_pending_changes() if c.get("story_id") == story_id]
    if not pending:
        # An approval that expired or was invalidated before it was used is
        # authorised again (approval.approve_change keeps the earlier one).
        # Only the story's current (latest) exact change; never an earlier proposal.
        records = approval.list_changes_for_story(story_id)
        latest = max(records, key=lambda c: c.get("created_at", 0)) if records else None
        if latest is not None and approval.renewal_reason(latest):
            return latest
        raise HTTPException(
            status_code=409,
            detail=f"no exact change is currently pending approval for {story_id}",
        )
    # Same "most recent" tie-break change_service.py already uses.
    return max(pending, key=lambda c: c.get("created_at", 0))


def _start_agents(background_tasks: BackgroundTasks, change_record_id: str, ctx: AuthContext) -> None:
    """After an approval, or after a person recorded their item: the agents
    take the change set's next items (the runner re-checks everything)."""
    from jde_mcp_server import config_items

    from ..executors import runner

    record = approval._load(change_record_id)
    if record is None or not config_items.is_change_set(record):
        return
    if any(config_items.executor_of(i) == "agent" and i["id"] not in (record.get("item_delivery") or {})
           for i in config_items.items_of(record)):
        background_tasks.add_task(runner.run_in_background, change_record_id, ctx.identity.id,
                                  ctx.identity.display_name)


def _require_ready(change_id: str, company_id: str) -> None:
    """The same readiness rule as the screen (services/readiness.py)."""
    from ..services import readiness

    change = get_change_service().get_for_customer(change_id, company_id)
    reason = readiness.refusal(change) if change is not None else None
    if reason:
        raise HTTPException(status_code=409, detail=reason)


@router.post("/changes/{change_id}/approve-change", status_code=200)
def approve_exact_change(
    change_id: str, payload: GovernanceDecisionInput, background_tasks: BackgroundTasks,
    ctx: AuthContext = Depends(require_write_access)
) -> dict:
    """Gate 2 -- "Jade may execute this specific proposed change."
    approval.approve_change() is the authoritative approval record, not
    a copy of it. It refuses unless the company has an approval policy
    and the caller holds a role that policy allows."""
    _require_queued_change(change_id, ctx.customer_id)
    record = _pending_change_record(change_id)
    _require_ready(change_id, ctx.customer_id)
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
    _start_agents(background_tasks, record["change_id"], ctx)
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
    """What the delivery gate would decide right now, check by check.
    Read-only: nothing is sent to JDE and nothing is recorded."""
    _require_queued_change(change_id, ctx.customer_id)
    return PreflightResult.model_validate(approval.preflight(_change_record_for(change_id)["change_id"]))


_DELIVERY_REFUSALS = (delivery.DeliveryRefused, approval.ChangeApprovalError, BindingInvalid, ScopeViolation,
                      CapabilityError, authority.AuthorityRevoked, authority.AuthorityUnverifiable)


def _delivery_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (approval.ApproverNotAuthorised, authority.AuthorityRevoked)):
        return HTTPException(status_code=403, detail=str(exc))
    return HTTPException(status_code=409, detail=str(exc))


def _own_change_record(change_id: str, ctx: AuthContext) -> dict:
    _require_queued_change(change_id, ctx.customer_id)
    record = _change_record_for(change_id)
    if record.get("company_id") != ctx.customer_id:
        raise HTTPException(status_code=404, detail=f"no such change: {change_id}")
    return record


@router.post("/changes/{change_id}/delivery/applied")
def record_applied(change_id: str, payload: RecordAppliedInput, background_tasks: BackgroundTasks,
                   ctx: AuthContext = Depends(require_write_access)) -> dict:
    """The Application Manager applied an item marked for a person (or an
    agent item no agent can apply now) in DEV. Jade re-checks the approval,
    scope and authority, reads the value back live and records it only if
    it is the approved value; then the agents continue with the next items."""
    record = _own_change_record(change_id, ctx)
    try:
        result = delivery.record_applied(record["change_id"], actor_user_id=ctx.identity.id,
                                         actor_name=ctx.identity.display_name,
                                         evidence_reference=payload.evidence_reference, note=payload.note,
                                         stated_value=payload.stated_value, item_id=payload.item_id,
                                         stated_values=payload.stated_values,
                                         confirmed_as_specified=payload.confirmed_as_specified)
    except _DELIVERY_REFUSALS as exc:
        raise _delivery_error(exc)
    if result.get("outcome") == "item_applied":
        _start_agents(background_tasks, record["change_id"], ctx)
    return result


@router.post("/changes/{change_id}/delivery/run-test")
def run_test(change_id: str, ctx: AuthContext = Depends(require_write_access)) -> dict:
    """Run the approved test orchestration live on the customer's AIS."""
    record = _own_change_record(change_id, ctx)
    try:
        return delivery.run_test(record["change_id"], actor_user_id=ctx.identity.id,
                                 actor_name=ctx.identity.display_name)
    except _DELIVERY_REFUSALS as exc:
        raise _delivery_error(exc)


@router.post("/changes/{change_id}/delivery/test-result")
def record_test_result(change_id: str, payload: RecordTestResultInput,
                       ctx: AuthContext = Depends(require_write_access)) -> dict:
    """Record the test result against the acceptance criteria (no
    orchestration, or instead of running it)."""
    record = _own_change_record(change_id, ctx)
    try:
        return delivery.record_test_result(record["change_id"], actor_user_id=ctx.identity.id,
                                           actor_name=ctx.identity.display_name, passed=payload.passed,
                                           evidence_reference=payload.evidence_reference, note=payload.note)
    except _DELIVERY_REFUSALS as exc:
        raise _delivery_error(exc)


@router.post("/changes/{change_id}/execution/reconcile")
def reconcile_write(
    change_id: str, payload: ReconcileWriteInput, ctx: AuthContext = Depends(require_write_access)
) -> dict:
    """Settle a write of unknown outcome (a record from before the recorded
    delivery route) by the ACTUAL target value: read live where the
    connection can, otherwise the value a person read in JDE, with a note
    and an evidence reference."""
    _require_queued_change(change_id, ctx.customer_id)
    _require_policy_approver(ctx)
    record = _change_record_for(change_id)
    if record.get("company_id") != ctx.customer_id:
        raise HTTPException(status_code=404, detail=f"no such change: {change_id}")
    op = record["operation"]
    from ..delivery import live

    try:
        read = live.read_processing_option(record["company_id"], record["story_id"], ctx.identity.id,
                                           op["application"], op["version"], op["option"])
        if not read.found:
            raise live.LiveUnavailable(read.detail)
        observed, source = read.value, f"live AIS read ({read.observation_id})"
        evidence_reference = f"live read {read.observation_id} of {op['application']}/{op['version']}/{op['option']}"
    except live.LiveUnavailable as exc:
        if payload.observed_value is None or not payload.note.strip() or not payload.evidence_reference.strip():
            raise HTTPException(
                status_code=422, detail=f"Jade cannot read the value live ({exc}). Provide observedValue, a note and "
                                        "an evidenceReference.")
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


@router.post("/changes/{change_id}/resolve-without-change", status_code=200)
def confirm_resolved_without_change(
    change_id: str, payload: GovernanceDecisionInput, ctx: AuthContext = Depends(require_write_access)
) -> dict:
    """The Application Manager confirms the Architect's finding that no JD
    Edwards change is needed (Section 15.6). Audit 23 Oct (F00): this does
    NOT close the story. Any work the requirement still needs is handed to
    people and recorded; the business accepts the outcome; the acceptance
    closes it. JD Edwards is never touched."""
    from ..delivery import handoff

    change = _require_queued_change(change_id, ctx.customer_id)
    _require_ready(change_id, ctx.customer_id)
    try:
        handoff.confirm_no_change(change, actor_name=ctx.identity.display_name, actor_user_id=ctx.identity.id,
                                  roles=set(ctx.roles), note=payload.note or "")
    except handoff.HandoffRefused as exc:
        status = 403 if "only the Application Manager" in str(exc) else 409
        raise HTTPException(status_code=status, detail=str(exc))
    get_decision_feedback_service().record(change_id=change_id, customer_id=ctx.customer_id,
                                           kind="resolved_without_change", decided_by=ctx.identity.display_name,
                                           identity_id=ctx.identity.id, note=payload.note)
    out = get_change_service().get_for_customer(change_id, ctx.customer_id)
    assert out is not None
    return out.model_dump(mode="json", by_alias=True)


class ReopenInput(ApiModel):
    reason: str


@router.post("/changes/{change_id}/reopen-resolved", status_code=200)
def reopen_resolved(change_id: str, payload: ReopenInput, ctx: AuthContext = Depends(require_write_access)) -> dict:
    """Deliberately reopen a story an earlier workflow closed as resolved
    without change before its work was done or the business accepted it.
    Recorded with who, when and why; the earlier closure stays in history."""
    from jde_mcp_server import backlog
    from jde_mcp_server.evidence import capture_evidence

    if not ({"product_manager", "admin"} & set(ctx.roles)):
        raise HTTPException(status_code=403, detail="only the Application Manager reopens a resolved story")
    if get_change_service().get_for_customer(change_id, ctx.customer_id) is None:
        raise HTTPException(status_code=404, detail="This story could not be found.")
    try:
        backlog.reopen_resolved(change_id, payload.reason, ctx.identity.display_name)
    except backlog.BacklogError as exc:
        raise HTTPException(status_code=409 if "only a story" in str(exc) else 422, detail=str(exc))
    try:
        capture_evidence(change_id, {"event": "reopened", "stage": "closure", "actor": ctx.identity.display_name,
                                     "detail": f"reopened to finish the work: {payload.reason.strip()}"})
    except Exception:  # noqa: BLE001
        pass
    out = get_change_service().get_for_customer(change_id, ctx.customer_id)
    assert out is not None
    return out.model_dump(mode="json", by_alias=True)
