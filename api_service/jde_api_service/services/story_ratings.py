"""Read-only planning ratings and version-bound human confirmation.
Ratings never participate in an approval, route, lifecycle or execution decision.
"""
import hashlib
import json
from ..models.ratings import RatingView, StoryRatings


def view(change, review=None):
    story = review.history[-1].user_story if review and review.history else change.user_story
    business_source = {"story": change.user_story.model_dump(mode="json") if change.user_story else None,
                       "reviewed_story": story.model_dump(mode="json") if story else None,
                       "story_revision": len(review.history) if review else 0,
                       "domain": change.business_domain_id}
    technical_source = {"business": business_source,
                        "architecture": change.architect_decision.model_dump(mode="json") if change.architect_decision else None,
                        "spec": change.implementation_spec.model_dump(mode="json") if change.implementation_spec else None}
    history = review.rating_confirmations if review else []
    def rating(key, proposal, source):
        digest = hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest()
        last = next((r for r in reversed(history) if r.key == key), None)
        current = last is not None and last.source_hash == digest
        return RatingView(proposed=proposal, confirmed=last.value if current else None,
                          status="confirmed" if current else "stale" if last else "proposed" if proposal else "not_assessed",
                          source_hash=digest, confirmed_by=last.actor if current else None,
                          confirmed_at=last.at if current else None)
    return StoryRatings(
        business_impact=rating("business_impact", story.business_impact_rating if story else None, business_source),
        business_benefit=rating("business_benefit", story.business_benefit_rating if story else None, business_source),
        technical_impact=rating("technical_impact", change.architect_decision.technical_impact_rating if change.architect_decision else None, technical_source),
        revision=len(history))


def attach(change):
    if change.user_story:
        from .registry import get_domain_review_service
        change.ratings = view(change, get_domain_review_service().get(change.id))
    return change
