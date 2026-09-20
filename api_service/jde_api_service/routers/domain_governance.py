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
from ..dependencies import AuthContext, require_customer_access
from ..models.business_domain import BusinessDomain
from ..models.delivery_queue import DeliveryQueueEntry
from ..models.domain_review import (
    AssignDomainInput,
    DomainOwnerEditInput,
    DomainReview,
    GovernanceDecisionInput,
)
from ..services.architecture_driver import run_architecture_review
from ..services.registry import (
    get_architecture_review_service,
    get_business_domain_service,
    get_change_service,
    get_delivery_queue_service,
    get_domain_review_service,
)
from ..services.review_driver import ReviewerAgentError, run_reviewer_agent

router = APIRouter(tags=["domain-governance"])


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
    change_id: str, payload: AssignDomainInput, ctx: AuthContext = Depends(require_customer_access)
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
        )
    except ReviewerAgentError as exc:
        # Fails visibly, not silently: the stage stays at
        # domain_owner_requested_revision (the edit is safe, recorded
        # history) rather than either advancing or discarding it.
        service.set_stage(change_id, "domain_owner_requested_revision")
        raise HTTPException(status_code=502, detail=f"reviewer agent failed: {exc}") from exc

    service.append_version(change_id, label="reviewer_agent_revision", user_story=revised, actor="Reviewer Agent")
    return service.set_stage(change_id, "domain_owner_reviewing")


@router.post("/changes/{change_id}/domain-review/approve", response_model=DomainReview)
def domain_owner_approve(
    change_id: str, payload: GovernanceDecisionInput, ctx: AuthContext = Depends(require_customer_access)
) -> DomainReview:
    _change_with_story(change_id, ctx.customer_id)
    service = get_domain_review_service()
    review = service.get(change_id)
    if review is None or review.stage != "domain_owner_reviewing":
        raise HTTPException(status_code=409, detail=f"cannot approve from stage {review.stage if review else 'none'}")
    return service.record_domain_owner_approval(change_id, payload.decided_by, payload.note)


@router.post("/changes/{change_id}/domain-review/application-manager-approve", response_model=DomainReview)
def application_manager_approve(
    change_id: str,
    payload: GovernanceDecisionInput,
    background_tasks: BackgroundTasks,
    ctx: AuthContext = Depends(require_customer_access),
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

    updated = service.record_application_manager_approval(change_id, payload.decided_by, payload.note)
    backlog.approve(change_id, payload.decided_by, payload.note)
    get_delivery_queue_service().add(change_id, ctx.customer_id, payload.decided_by, payload.note)

    background_tasks.add_task(
        run_architecture_review,
        story_id=change_id,
        repo_root=settings.repo_root,
        run_service=get_architecture_review_service(),
    )
    return updated


@router.get("/delivery-queue", response_model=list[DeliveryQueueEntry])
def list_delivery_queue(ctx: AuthContext = Depends(require_customer_access)) -> list[DeliveryQueueEntry]:
    """The set of approved changes Jade is authorised to work on,
    in queue order. Not a Sprint: no capacity, no start/end date --
    just an ordered list a human (the Application Manager) put entries
    into, one at a time, via application_manager_approve above."""
    return get_delivery_queue_service().list_for_customer(ctx.customer_id)
