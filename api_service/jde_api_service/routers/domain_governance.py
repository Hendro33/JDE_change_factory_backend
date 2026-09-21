"""
Business Domain ownership and the Domain Owner / Application Manager
governance workflow (Increment: domain-aware governance).

Two human decisions, deliberately kept separate (never merged into one
generic "approve" the way Section 6.5 already keeps story approval and
exact-change approval separate):

  - Domain Owner approval: "Is the business requirement and User Story
    correct?" Recorded here only -- never calls into mcp_server, and
    never implies the work is authorised to proceed.
  - Application Manager approval: "Is this approved work that Jade is
    authorised to deliver?" This is the ONLY action in this router
    that calls backlog.approve() (mcp_server, unmodified) -- exactly
    the existing Gate 2 control, now reached through a richer upstream
    process instead of directly from BACKLOG_READY -- and it is also
    the action that adds the change to the Delivery Queue
    (delivery_queue_service.py). Jade has no Sprint concept: there is
    no planning ceremony, capacity or start/end date here, just an
    ordered queue of work a human has authorised.
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from jde_mcp_server import backlog

from ..config import settings
from ..dependencies import (
    AuthContext,
    require_customer_access,
    require_domain_owner_access,
    require_role,
    require_write_access,
)
from ..models.business_domain import BusinessDomain
from ..models.delivery_queue import DeliveryQueueEntry
from ..models.domain_review import (
    AskAboutRequirementInput,
    AssignDomainInput,
    DomainOwnerEditInput,
    DomainReview,
    GovernanceDecisionInput,
)
from ..services.architecture_driver import run_architecture_review
from ..services.conversation_driver import ConversationError, ask_about_requirement
from ..services.registry import (
    get_architecture_review_service,
    get_business_domain_service,
    get_change_service,
    get_decision_feedback_service,
    get_delivery_queue_service,
    get_domain_review_service,
)
from ..services.review_driver import ReviewerAgentError, run_reviewer_agent

# Stages past Domain Owner approval from which a human may explicitly
# flag "this requirement may need reconsideration" (Architecture
# Review's "Ask Jade about this requirement", typically) -- see
# request_requirement_reconsideration below. A proposed_amendment
# returned by /ask from ANY of these stages is display-only: the
# existing /domain-review/edit endpoint that would apply it already
# refuses outside domain_owner_reviewing, so it can never be applied
# directly from here -- this set only gates the explicit reopen action.
_RECONSIDERABLE_STAGES = {"domain_owner_approved", "ready_for_application_manager", "application_manager_approved"}

router = APIRouter(tags=["domain-governance"])


def _require_domain_owner_or_product_manager(ctx: AuthContext) -> None:
    """"Ask Jade about this requirement" is asked by either a Domain
    Owner (mid review) or a Product Manager (on an already-approved
    requirement) -- see ask_about_requirement_endpoint's own docstring.
    Deliberately not domain-scoped even for a Domain Owner caller: this
    only ever produces a conversation turn and, at most, a proposed
    amendment that is never applied here (see that docstring), so the
    risk this guards against is "may this identity ask at all," not
    "which domain" -- unlike start/edit/approve/reject below, which
    actually change governance state and stay domain-scoped."""
    if not (ctx.roles & {"domain_owner", "product_manager"}):
        raise HTTPException(status_code=403, detail="requires the Domain Owner or Product Manager role")


@router.get("/business-domains", response_model=list[BusinessDomain])
def list_business_domains(ctx: AuthContext = Depends(require_customer_access)) -> list[BusinessDomain]:
    return get_business_domain_service().list_for_customer(ctx.customer_id)


def _change_with_story(change_id: str, customer_id: str):
    change = get_change_service().get_for_customer(change_id, customer_id)
    if change is None:
        raise HTTPException(status_code=404, detail=f"no such change: {change_id}")
    if change.user_story is None:
        raise HTTPException(
            status_code=404,
            detail=f"{change_id} has no user story yet -- domain review starts once a story exists",
        )
    return change


@router.get("/changes/{change_id}/domain-review", response_model=DomainReview)
def get_domain_review(change_id: str, ctx: AuthContext = Depends(require_customer_access)) -> DomainReview:
    change = _change_with_story(change_id, ctx.customer_id)
    # Lazily materialised on first read, once a story actually exists to
    # review -- there is no separate "create" step the frontend has to
    # remember to call first.
    return get_domain_review_service().ensure(change_id, change.user_story)


@router.post("/changes/{change_id}/domain-review/assign-domain", response_model=DomainReview)
def assign_domain(
    change_id: str, payload: AssignDomainInput, ctx: AuthContext = Depends(require_write_access)
) -> DomainReview:
    change = _change_with_story(change_id, ctx.customer_id)
    get_domain_review_service().ensure(change_id, change.user_story)

    if not payload.uncertain and payload.business_domain_id:
        domain = get_business_domain_service().get_for_customer(payload.business_domain_id, ctx.customer_id)
        if domain is None:
            raise HTTPException(status_code=404, detail=f"no such business domain: {payload.business_domain_id}")

    return get_domain_review_service().assign_domain(
        change_id,
        business_domain_id=payload.business_domain_id,
        uncertain=payload.uncertain,
        note=payload.note,
    )


@router.post("/changes/{change_id}/domain-review/start", response_model=DomainReview)
def start_domain_owner_review(
    change_id: str, payload: GovernanceDecisionInput, ctx: AuthContext = Depends(require_customer_access)
) -> DomainReview:
    change = _change_with_story(change_id, ctx.customer_id)
    review = get_domain_review_service().ensure(change_id, change.user_story)
    require_domain_owner_access(ctx, review.business_domain_id)

    if review.stage == "domain_owner_reviewing":
        return review  # idempotent -- already started
    if review.stage != "ready_for_domain_owner":
        raise HTTPException(status_code=409, detail=f"cannot start review from stage {review.stage}")
    return get_domain_review_service().set_stage(change_id, "domain_owner_reviewing")


@router.post("/changes/{change_id}/domain-review/edit", response_model=DomainReview)
async def submit_domain_owner_edit(
    change_id: str, payload: DomainOwnerEditInput, ctx: AuthContext = Depends(require_customer_access)
) -> DomainReview:
    """Implements the required workflow rule: a manual Domain Owner
    edit never becomes the approved story by itself. This call always
    records the edit AND routes it through the Reviewer Agent before
    returning -- there is no way to reach domain_owner_approved from
    here without a Reviewer Agent pass having happened in between."""
    _change_with_story(change_id, ctx.customer_id)
    service = get_domain_review_service()
    review = service.get(change_id)
    if review is None or review.stage != "domain_owner_reviewing":
        raise HTTPException(
            status_code=409,
            detail="can only submit an edit while the Domain Owner review is in progress -- call .../start first",
        )
    require_domain_owner_access(ctx, review.business_domain_id)
    # The count carried by the version just before this edit cycle --
    # the honest "how many revisions has this story actually been
    # through" reference point, captured before any of this call's own
    # appends change what history[-1] points to.
    prior_revision_count = review.history[-1].user_story.revision_count if review.history else 0

    service.append_version(
        change_id, label="domain_owner_edit", user_story=payload.user_story, actor=payload.edited_by, note=payload.note
    )
    service.set_stage(change_id, "domain_owner_requested_revision")
    service.set_stage(change_id, "reviewer_agent_refining")

    try:
        revised = await run_reviewer_agent(
            story_id=change_id,
            edited_story=payload.user_story,
            domain_owner_note=payload.note,
            repo_root=settings.repo_root,
            customer_id=ctx.customer_id,
        )
    except ReviewerAgentError as exc:
        # Fails visibly, not silently: the stage stays at
        # domain_owner_requested_revision (the edit is safe, recorded
        # history) rather than either advancing or discarding it.
        service.set_stage(change_id, "domain_owner_requested_revision")
        raise HTTPException(status_code=502, detail=f"reviewer agent failed: {exc}") from exc

    # Set deterministically here rather than trusted from the reviewer
    # agent's own summary: run_reviewer_agent's prompt never tells it
    # the prior count, so its self-reported revision_count has no real
    # continuity across a Domain Owner edit cycle (unlike the initial
    # Receive/Improve/Check loop, where the same agent run tracks its
    # own attempts). NOTE (honest limitation, not fixed here): this
    # updates the count on THIS DomainReview.history entry only --
    # Change.user_story is still assembled from EnhancementRun
    # (change_service.py), which this governance-stage revision never
    # touches, so this count is not yet reflected in FactoryMetrics'
    # first_time_success_rate for a story that has been through a
    # Domain Owner edit. Folding DomainReview history back into the
    # canonical Change.user_story is a larger assembly-logic change,
    # out of scope for this increment.
    revised.revision_count = prior_revision_count + 1

    service.append_version(change_id, label="reviewer_agent_revision", user_story=revised, actor="Reviewer Agent")
    get_decision_feedback_service().record(
        change_id=change_id,
        customer_id=ctx.customer_id,
        kind="domain_owner_edit",
        decided_by=payload.edited_by,
        identity_id=ctx.identity.id,
        note=payload.note,
    )
    return service.set_stage(change_id, "domain_owner_reviewing")


@router.post("/changes/{change_id}/domain-review/ask", response_model=DomainReview)
async def ask_about_requirement_endpoint(
    change_id: str, payload: AskAboutRequirementInput, ctx: AuthContext = Depends(require_customer_access)
) -> DomainReview:
    """"Ask Jade about this requirement" -- requirement collaboration,
    not a general chatbot; every turn is scoped to this one requirement
    and answered by the same improve-agent-backed driver either a
    Domain Owner (mid review) or an Application Manager (on an
    already-approved requirement, from Architecture Review) asks.

    Explanation never touches the requirement -- it is only ever
    recorded as a conversation turn. A proposed amendment is recorded
    the same way and returned for review; it is NEVER applied here.
    Applying one is a separate, explicit human action through the
    EXISTING /domain-review/edit endpoint, which already refuses
    outside domain_owner_reviewing -- so a proposed_amendment surfaced
    to an Application Manager on an already-approved requirement can be
    seen, never silently applied. See request_requirement_reconsideration
    below for what an Application Manager does with one instead."""
    _require_domain_owner_or_product_manager(ctx)
    _change_with_story(change_id, ctx.customer_id)
    service = get_domain_review_service()
    review = service.get(change_id)
    if review is None or not review.history:
        raise HTTPException(status_code=409, detail="no requirement to discuss yet for this change")
    if not payload.question.strip():
        raise HTTPException(status_code=422, detail="a question is required")

    current_story = review.history[-1].user_story
    try:
        result = await ask_about_requirement(
            story_id=change_id,
            current_story=current_story,
            question=payload.question,
            asked_by=payload.asked_by,
            prior_turns=review.conversation,
            repo_root=settings.repo_root,
            customer_id=ctx.customer_id,
        )
    except ConversationError as exc:
        raise HTTPException(status_code=502, detail=f"could not get an answer: {exc}") from exc

    return service.append_conversation_turn(
        change_id,
        asked_by=payload.asked_by,
        question=payload.question,
        answer=result["answer"],
        kind=result["kind"],
        proposed_user_story=result["proposed_user_story"],
        identity_id=ctx.identity.id,
    )


@router.post("/changes/{change_id}/domain-review/request-reconsideration", response_model=DomainReview)
def request_requirement_reconsideration(
    change_id: str, payload: GovernanceDecisionInput, ctx: AuthContext = Depends(require_role("product_manager"))
) -> DomainReview:
    """The only way back from past Domain Owner approval: an
    Application Manager, having asked Jade about the already-approved
    requirement and received a proposed_amendment (or having their own
    reason), explicitly flags that it may need to be reconsidered.
    Reopens the SAME domain_owner_reviewing stage the original review
    used -- no new lifecycle state -- so the Domain Owner's next
    decision goes through exactly the existing governed/versioned flow
    (approve again, edit, or reject). Never automatic: this is the
    explicit human action the design requires instead of silently
    amending a business-approved requirement."""
    _change_with_story(change_id, ctx.customer_id)
    service = get_domain_review_service()
    review = service.get(change_id)
    if review is None or review.stage not in _RECONSIDERABLE_STAGES:
        raise HTTPException(
            status_code=409,
            detail=f"cannot request reconsideration from stage {review.stage if review else 'none'}",
        )
    if not payload.note:
        raise HTTPException(status_code=422, detail="explain why this requirement needs reconsideration")

    updated = service.request_reconsideration(change_id, requested_by=payload.decided_by, note=payload.note, identity_id=ctx.identity.id)
    get_decision_feedback_service().record(
        change_id=change_id,
        customer_id=ctx.customer_id,
        kind="requirement_reconsideration_requested",
        decided_by=payload.decided_by,
        identity_id=ctx.identity.id,
        note=payload.note,
    )
    return updated


@router.post("/changes/{change_id}/domain-review/approve", response_model=DomainReview)
def domain_owner_approve(
    change_id: str, payload: GovernanceDecisionInput, ctx: AuthContext = Depends(require_customer_access)
) -> DomainReview:
    _change_with_story(change_id, ctx.customer_id)
    service = get_domain_review_service()
    review = service.get(change_id)
    if review is None or review.stage != "domain_owner_reviewing":
        raise HTTPException(status_code=409, detail=f"cannot approve from stage {review.stage if review else 'none'}")
    require_domain_owner_access(ctx, review.business_domain_id)
    updated = service.record_domain_owner_approval(change_id, payload.decided_by, payload.note, identity_id=ctx.identity.id)
    get_decision_feedback_service().record(
        change_id=change_id,
        customer_id=ctx.customer_id,
        kind="domain_owner_approval",
        decided_by=payload.decided_by,
        identity_id=ctx.identity.id,
        note=payload.note,
    )
    return updated


@router.post("/changes/{change_id}/domain-review/reject", response_model=DomainReview)
def domain_owner_reject(
    change_id: str, payload: GovernanceDecisionInput, ctx: AuthContext = Depends(require_customer_access)
) -> DomainReview:
    """The Domain Owner's other real decision alongside approve/edit: the
    requirement itself should not proceed. Distinct from an edit
    (which stays in play, routed through the Reviewer Agent) -- this
    is terminal and, like every rejection in this system, requires a
    reason. Recorded entirely in this sidecar; never calls into
    mcp_server, the same as domain_owner_approve above."""
    _change_with_story(change_id, ctx.customer_id)
    service = get_domain_review_service()
    review = service.get(change_id)
    if review is None or review.stage != "domain_owner_reviewing":
        raise HTTPException(status_code=409, detail=f"cannot reject from stage {review.stage if review else 'none'}")
    require_domain_owner_access(ctx, review.business_domain_id)
    if not payload.note:
        raise HTTPException(status_code=422, detail="a rejection must include a reason")
    updated = service.record_domain_owner_rejection(change_id, payload.decided_by, payload.note, identity_id=ctx.identity.id)
    get_decision_feedback_service().record(
        change_id=change_id,
        customer_id=ctx.customer_id,
        kind="domain_owner_rejection",
        decided_by=payload.decided_by,
        identity_id=ctx.identity.id,
        reason_code=payload.rejection_reason,
        note=payload.note,
    )
    return updated


@router.post("/changes/{change_id}/domain-review/application-manager-approve", response_model=DomainReview)
def application_manager_approve(
    change_id: str,
    payload: GovernanceDecisionInput,
    background_tasks: BackgroundTasks,
    ctx: AuthContext = Depends(require_role("product_manager")),
) -> DomainReview:
    """Gate 1 -- "Jade may start working on this requirement." The one
    action that actually clears backlog.py's Gate 2 (unmodified) --
    Domain Owner approval never does this. Reaching this endpoint's
    success path means: a human explicitly approved the business
    requirement (Domain Owner) AND a human explicitly authorised it
    for delivery (Application Manager here) -- two named people, two
    timestamps, two reasons, exactly Section 6.5's model.

    Also starts Architecture Review as a background task, the same
    BackgroundTasks mechanism /changes/{id}/enhance already uses --
    once Gate 1 has authorised the work, Jade begins architecture
    analysis on its own rather than waiting for a second, separate
    button press. This is presentational scheduling only: it never
    calls propose_change or approve_change itself, and Gate 2 (exact
    change approval) still requires its own explicit human decision
    below, same as before."""
    _change_with_story(change_id, ctx.customer_id)
    service = get_domain_review_service()
    review = service.get(change_id)
    if review is None or review.stage != "ready_for_application_manager":
        raise HTTPException(status_code=409, detail=f"cannot approve from stage {review.stage if review else 'none'}")

    updated = service.record_application_manager_approval(
        change_id, payload.decided_by, payload.note, identity_id=ctx.identity.id
    )
    backlog.approve(change_id, payload.decided_by, payload.note)
    get_delivery_queue_service().add(change_id, ctx.customer_id, payload.decided_by, payload.note)
    get_decision_feedback_service().record(
        change_id=change_id,
        customer_id=ctx.customer_id,
        kind="application_manager_approval",
        decided_by=payload.decided_by,
        identity_id=ctx.identity.id,
        note=payload.note,
    )

    background_tasks.add_task(
        run_architecture_review,
        story_id=change_id,
        repo_root=settings.repo_root,
        run_service=get_architecture_review_service(),
        customer_id=ctx.customer_id,
    )
    return updated


@router.post("/changes/{change_id}/domain-review/application-manager-reject", response_model=DomainReview)
def application_manager_reject(
    change_id: str, payload: GovernanceDecisionInput, ctx: AuthContext = Depends(require_role("product_manager"))
) -> DomainReview:
    """Gate 1 rejection -- "Jade is not authorised to work on this,"
    the other real outcome alongside application_manager_approve
    above. Calls backlog.reject() (mcp_server, unmodified) -- the same
    real Gate 2 control approval calls backlog.approve() on, so the
    Change's own state genuinely reflects the rejection (REJECTED),
    not just this sidecar. No Delivery Queue entry is ever created for
    a rejected story, and no Architecture Review is scheduled."""
    _change_with_story(change_id, ctx.customer_id)
    service = get_domain_review_service()
    review = service.get(change_id)
    if review is None or review.stage != "ready_for_application_manager":
        raise HTTPException(status_code=409, detail=f"cannot reject from stage {review.stage if review else 'none'}")
    if not payload.note:
        raise HTTPException(status_code=422, detail="a rejection must include a reason")

    updated = service.record_application_manager_rejection(
        change_id, payload.decided_by, payload.note, identity_id=ctx.identity.id
    )
    backlog.reject(change_id, payload.decided_by, payload.note)
    get_decision_feedback_service().record(
        change_id=change_id,
        customer_id=ctx.customer_id,
        kind="application_manager_rejection",
        decided_by=payload.decided_by,
        identity_id=ctx.identity.id,
        reason_code=payload.rejection_reason,
        note=payload.note,
    )
    return updated


@router.get("/delivery-queue", response_model=list[DeliveryQueueEntry])
def list_delivery_queue(ctx: AuthContext = Depends(require_customer_access)) -> list[DeliveryQueueEntry]:
    """The set of approved changes Jade is authorised to work on,
    in queue order. Not a Sprint: no capacity, no start/end date --
    just an ordered list a human (the Application Manager) put entries
    into, one at a time, via application_manager_approve above."""
    return get_delivery_queue_service().list_for_customer(ctx.customer_id)
