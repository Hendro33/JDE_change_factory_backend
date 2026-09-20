"""
DomainReview -- the Domain Owner / Application Manager governance
sidecar for one Change (Increment: business domain ownership).

Deliberately a SIDECAR, exactly like customer_link_service.py's own
story_id -> customer_id mapping: it lives entirely in api_service's own
data directory, never inside mcp_server's backlog.py record. That
keeps every existing Gate 2 control (backlog.py, Section 3.5) and the
exact-change control (approval.py, Section 6.5) completely unchanged --
this module adds a richer, earlier governance stage in FRONT of them,
and only ever calls backlog.approve()/reject() (unmodified) once an
Application Manager gives the final sprint/build decision.

The workflow rule this exists to enforce (never silently promote a
manual edit to "the approved story"):

    Domain Owner edit -> Reviewer Agent -> revised User Story -> Domain Owner approval

`history` is append-only evidence: the original AI-generated version,
every Domain Owner edit, and every Reviewer Agent revision are all kept,
never overwritten -- the same "evidence, not a status flag" principle
Section 6.4/6.5 already applies to ApprovalRecord.
"""

from __future__ import annotations

from typing import Literal, Optional

from .base import ApiModel
from .change import ApprovalRecord, UserStory

DomainReviewStage = Literal[
    "ready_for_domain_owner",
    "domain_owner_reviewing",
    "domain_owner_requested_revision",
    "reviewer_agent_refining",
    "domain_owner_approved",
    "ready_for_application_manager",
    "application_manager_approved",
]

StoryVersionLabel = Literal["ai_generated", "domain_owner_edit", "reviewer_agent_revision"]


class StoryVersion(ApiModel):
    label: StoryVersionLabel
    user_story: UserStory
    note: str = ""
    actor: str = ""
    captured_at: str


class DomainReview(ApiModel):
    change_id: str
    business_domain_id: Optional[str] = None
    # Section 2's "expose uncertainty rather than inventing a
    # classification" -- set when a human has looked and genuinely
    # cannot place the request in an existing domain (e.g. T009).
    domain_classification_uncertain: bool = False
    domain_classification_note: str = ""
    stage: DomainReviewStage = "ready_for_domain_owner"
    history: list[StoryVersion] = []
    domain_owner_approval: Optional[ApprovalRecord] = None
    application_manager_approval: Optional[ApprovalRecord] = None
    updated_at: str


# ---------------------------------------------------------------------
# Request payloads (routers/domain_governance.py). Every decision is
# recorded against a named person, same convention DecisionInput
# already establishes for story/change approval.
# ---------------------------------------------------------------------
class AssignDomainInput(ApiModel):
    business_domain_id: Optional[str] = None
    uncertain: bool = False
    note: str = ""


class GovernanceDecisionInput(ApiModel):
    decided_by: str
    note: str = ""


class DomainOwnerEditInput(ApiModel):
    edited_by: str
    note: str = ""
    user_story: UserStory
