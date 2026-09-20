"""
DomainReview persistence and the small set of state transitions the
Domain Owner / Application Manager governance flow needs. See
models/domain_review.py for why this is a sidecar and never touches
mcp_server.

Every mutator here does ONE thing and saves -- the workflow sequencing
itself (edit -> call the Reviewer Agent -> record its revision) lives
in the router, the same split responsibility change_service.py /
orchestration_driver.py already have with the routes that call them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ..models.change import ApprovalRecord, UserStory
from ..models.domain_review import DomainReview, DomainReviewStage, StoryVersion
from ..persistence.json_file_store import JsonFileStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DomainReviewError(RuntimeError):
    pass


class DomainReviewService:
    def __init__(self, directory: str) -> None:
        self._store = JsonFileStore(directory)

    def get(self, change_id: str) -> Optional[DomainReview]:
        doc = self._store.get(change_id)
        return DomainReview.model_validate(doc) if doc else None

    def ensure(self, change_id: str, ai_generated_story: UserStory) -> DomainReview:
        """Idempotent: once a story reaches the backlog, a DomainReview
        exists for it, seeded with the AI-generated version as history
        entry 1 -- never re-seeded or overwritten on subsequent calls."""
        existing = self.get(change_id)
        if existing is not None:
            return existing
        review = DomainReview(
            change_id=change_id,
            stage="ready_for_domain_owner",
            history=[
                StoryVersion(
                    label="ai_generated",
                    user_story=ai_generated_story,
                    actor="Check Agent",
                    captured_at=_now(),
                )
            ],
            updated_at=_now(),
        )
        self._save(review)
        return review

    def _require(self, change_id: str) -> DomainReview:
        review = self.get(change_id)
        if review is None:
            raise DomainReviewError(f"no domain review record for {change_id}")
        return review

    def latest_story(self, change_id: str) -> Optional[UserStory]:
        review = self.get(change_id)
        if review is None or not review.history:
            return None
        return review.history[-1].user_story

    def assign_domain(
        self,
        change_id: str,
        *,
        business_domain_id: Optional[str],
        uncertain: bool,
        note: str = "",
    ) -> DomainReview:
        review = self._require(change_id)
        # Assigning a domain and flagging uncertainty are mutually
        # exclusive on purpose (Section 2: "expose uncertainty rather
        # than inventing a classification") -- a record is either
        # confidently placed or honestly not, never both at once.
        review.business_domain_id = None if uncertain else business_domain_id
        review.domain_classification_uncertain = uncertain
        review.domain_classification_note = note
        review.updated_at = _now()
        self._save(review)
        return review

    def set_stage(self, change_id: str, stage: DomainReviewStage) -> DomainReview:
        review = self._require(change_id)
        review.stage = stage
        review.updated_at = _now()
        self._save(review)
        return review

    def append_version(
        self,
        change_id: str,
        *,
        label: str,
        user_story: UserStory,
        actor: str,
        note: str = "",
    ) -> DomainReview:
        review = self._require(change_id)
        review.history.append(
            StoryVersion(label=label, user_story=user_story, actor=actor, note=note, captured_at=_now())  # type: ignore[arg-type]
        )
        review.updated_at = _now()
        self._save(review)
        return review

    def record_domain_owner_approval(
        self, change_id: str, approved_by: str, note: str = "", identity_id: Optional[str] = None
    ) -> DomainReview:
        review = self._require(change_id)
        review.domain_owner_approval = ApprovalRecord(
            approval_id=f"AP-{change_id}-DO",
            kind="domain_owner",
            status="approved",
            approved_by=approved_by,
            approved_at=_now(),
            note=note,
            identity_id=identity_id,
        )
        # Both stages are recorded (not just the second): "Domain Owner
        # approved" is what actually happened here, and "Ready for
        # Application Manager" is the immediate, automatic consequence
        # -- Domain Owner approval is never held pending some further
        # action before it hands off. Section 7's "do not imply Domain
        # Owner approval means approved for development" is enforced by
        # WHICH approval record this is (kind="domain_owner"), not by
        # withholding the handoff.
        review.stage = "domain_owner_approved"
        review.updated_at = _now()
        self._save(review)
        review.stage = "ready_for_application_manager"
        review.updated_at = _now()
        self._save(review)
        return review

    def record_application_manager_approval(
        self, change_id: str, approved_by: str, note: str = "", identity_id: Optional[str] = None
    ) -> DomainReview:
        review = self._require(change_id)
        review.application_manager_approval = ApprovalRecord(
            approval_id=f"AP-{change_id}-AM",
            kind="application_manager",
            status="approved",
            approved_by=approved_by,
            approved_at=_now(),
            note=note,
            identity_id=identity_id,
        )
        review.stage = "application_manager_approved"
        review.updated_at = _now()
        self._save(review)
        return review

    def record_domain_owner_rejection(
        self, change_id: str, decided_by: str, note: str = "", identity_id: Optional[str] = None
    ) -> DomainReview:
        """Terminal: the Domain Owner decided this requirement should
        not proceed at all -- distinct from record_domain_owner_approval
        above and from an edit (which stays in play). Reuses the same
        ApprovalRecord field the approval uses, just with status
        "rejected" -- exactly how ApprovalRecord already models a
        rejection everywhere else in this system (Section 6.5)."""
        review = self._require(change_id)
        review.domain_owner_approval = ApprovalRecord(
            approval_id=f"AP-{change_id}-DO",
            kind="domain_owner",
            status="rejected",
            approved_by=decided_by,
            approved_at=_now(),
            note=note,
            identity_id=identity_id,
        )
        review.stage = "domain_owner_rejected"
        review.updated_at = _now()
        self._save(review)
        return review

    def record_application_manager_rejection(
        self, change_id: str, decided_by: str, note: str = "", identity_id: Optional[str] = None
    ) -> DomainReview:
        """Terminal: Gate 1 rejection. The caller is responsible for
        also calling backlog.reject() (mcp_server, unmodified) -- this
        method only records the sidecar side, the same split
        record_application_manager_approval already has with
        backlog.approve()."""
        review = self._require(change_id)
        review.application_manager_approval = ApprovalRecord(
            approval_id=f"AP-{change_id}-AM",
            kind="application_manager",
            status="rejected",
            approved_by=decided_by,
            approved_at=_now(),
            note=note,
            identity_id=identity_id,
        )
        review.stage = "application_manager_rejected"
        review.updated_at = _now()
        self._save(review)
        return review

    def _save(self, review: DomainReview) -> None:
        self._store.put(review.change_id, review.model_dump(mode="json", by_alias=False))
