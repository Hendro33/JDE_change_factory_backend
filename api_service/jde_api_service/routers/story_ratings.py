from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from ..dependencies import AuthContext, require_customer_access, require_domain_owner_access, require_current_role
from ..models.ratings import ConfirmRatingInput, RatingConfirmation, StoryRatings
from ..services.registry import get_change_service, get_domain_review_service
from ..services import story_ratings

router = APIRouter(tags=["story-ratings"])
BUSINESS_STAGES = {"ready_for_domain_owner", "domain_owner_reviewing"}
TECHNICAL_STAGES = {"application_manager_approved"}


def load(change_id, ctx):
    change = get_change_service().get_for_customer(change_id, ctx.customer_id)
    if change is None or change.user_story is None:
        raise HTTPException(404, "No such user story")
    return change


def authorise(ctx, review, technical):
    if technical:
        require_current_role(ctx, "product_manager", "admin")
    else:
        require_domain_owner_access(ctx, review.business_domain_id)
    if review.stage not in (TECHNICAL_STAGES if technical else BUSINESS_STAGES):
        raise HTTPException(409, "This assessment is outside the current review stage; reopen the existing review if needed")


def response(change, review, ctx):
    result = story_ratings.view(change, review)
    if review:
        for technical, field in [(False, "can_confirm_business"), (True, "can_confirm_technical")]:
            try:
                authorise(ctx, review, technical)
                setattr(result, field, True)
            except HTTPException:
                pass
    return result


@router.get("/changes/{change_id}/ratings", response_model=StoryRatings)
def ratings(change_id: str, ctx: AuthContext = Depends(require_customer_access)):
    return response(load(change_id, ctx), get_domain_review_service().get(change_id), ctx)


@router.post("/changes/{change_id}/ratings/confirm", response_model=StoryRatings)
def confirm(change_id: str, payload: ConfirmRatingInput, ctx: AuthContext = Depends(require_customer_access)):
    change = load(change_id, ctx)
    values = {"Small", "Medium", "High"} if payload.key == "business_benefit" else {"Low", "Medium", "High"}
    if payload.value not in values:
        raise HTTPException(422, "Rating is outside this scale")
    service = get_domain_review_service()
    # The same lock used by review edits/approvals; compare the version and re-check authority inside it.
    with service._store.locked():
        review = service.get(change_id)
        if review is None:
            raise HTTPException(409, "Open the existing story review before confirming ratings")
        authorise(ctx, review, payload.key == "technical_impact")
        change = load(change_id, ctx)
        current = story_ratings.view(change, review)
        if payload.expected_revision != current.revision or payload.source_hash != getattr(current, payload.key).source_hash:
            raise HTTPException(409, "The story, architecture or ratings changed. Reload before confirming")
        review.rating_confirmations.append(RatingConfirmation(key=payload.key, value=payload.value,
            source_hash=payload.source_hash, actor_id=ctx.identity.id, actor=ctx.identity.display_name,
            at=datetime.now(timezone.utc).isoformat()))
        service._save(review)
        return response(change, review, ctx)
