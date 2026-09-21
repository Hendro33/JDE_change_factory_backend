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
from .decision_feedback import FeedbackReasonCode

DomainReviewStage = Literal[
    "ready_for_domain_owner",
    "domain_owner_reviewing",
    "domain_owner_requested_revision",
    "reviewer_agent_refining",
    "domain_owner_approved",
    # Terminal: the Domain Owner decided the requirement itself should
    # not proceed -- distinct from a revision request, which stays in
    # play. Recorded entirely in this sidecar, same as approval; never
    # calls into mcp_server (Section 7's "Domain Owner never implies
    # authorisation to proceed" applies symmetrically to a rejection).
    "domain_owner_rejected",
    "ready_for_application_manager",
    "application_manager_approved",
    # Terminal: Gate 1 rejection. The only rejection stage that also
    # reaches mcp_server -- application_manager_reject() below calls
    # backlog.reject() (unmodified), the same real Gate 2 control
    # application_manager_approve() already calls backlog.approve() on.
    "application_manager_rejected",
]

StoryVersionLabel = Literal["ai_generated", "domain_owner_edit", "reviewer_agent_revision"]


class StoryVersion(ApiModel):
    label: StoryVersionLabel
    user_story: UserStory
    note: str = ""
    actor: str = ""
    captured_at: str


# ---------------------------------------------------------------------
# "Ask Jade about this requirement" (requirement collaboration -- not a
# generic chatbot; every turn is scoped to this one requirement).
# Reachable from User Story Review (Domain Owner, mid-review) and from
# Architecture Review (Application Manager, on an already-approved
# requirement). Answered by the SAME improve-agent-backed driver either
# way (conversation_driver.py) -- there is no separate "agent" for this,
# reusing Improve's own knowledge of the requirement.
#
# kind distinguishes the outcomes the design requires:
#   "explanation"          -- answers the question, changes nothing.
#   "proposed_amendment"   -- what was said looks like new information
#                              that should change the requirement. The
#                              draft is attached (proposed_user_story) but
#                              NEVER auto-applied. It only ever reaches the
#                              authoritative record if a human explicitly
#                              submits it through the EXISTING
#                              /domain-review/edit endpoint (which already
#                              enforces "only while domain_owner_reviewing,
#                              always through Improve, always versioned").
#                              Asked from Architecture Review, past Domain
#                              Owner approval, that endpoint is correctly
#                              unreachable (wrong stage) -- see
#                              request_requirement_reconsideration below
#                              for the only way forward from there.
#   "recommend_reanalysis" -- solution-side only ("Ask Jade about this
#                              solution", architecture_review.py's own
#                              ArchitectureReviewRun.conversation). The
#                              Architect can't produce an inline draft the
#                              way Improve does for a requirement -- a
#                              real re-analysis is a full Architecture
#                              Review run, so this kind only ever points
#                              back at the EXISTING manual retrigger
#                              endpoint (POST .../architecture-review),
#                              never applies anything itself.
# ---------------------------------------------------------------------
ConversationTurnKind = Literal["explanation", "proposed_amendment", "recommend_reanalysis"]


class ConversationTurn(ApiModel):
    turn_id: str
    asked_by: str
    question: str
    answer: str
    kind: ConversationTurnKind
    # Only present when kind == "proposed_amendment" -- a full draft
    # UserStory, never a partial diff, so the review surface is always
    # "here is the whole requirement as Jade would revise it."
    proposed_user_story: Optional[UserStory] = None
    asked_at: str
    identity_id: Optional[str] = None


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
    # Append-only, same evidence convention as history -- see
    # ConversationTurn's own docstring above.
    conversation: list[ConversationTurn] = []
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
    # Deliberately NO decided_by field -- who decided is derived
    # server-side from the authenticated session (ctx.identity.display_name
    # in routers/domain_governance.py), never trusted from the client.
    # See dependencies.py's own docstring on why identity is never a
    # client-supplied value.
    note: str = ""
    # Only meaningful on a rejection (approve-change ignores it).
    # Optional and additive to the existing free-text note -- a
    # structured signal future agent-improvement analysis (design doc
    # Section 14.2) can aggregate without parsing natural language.
    rejection_reason: Optional[FeedbackReasonCode] = None


class DomainOwnerEditInput(ApiModel):
    # No edited_by -- see GovernanceDecisionInput's own comment.
    note: str = ""
    user_story: UserStory


class AskAboutRequirementInput(ApiModel):
    # No asked_by -- see GovernanceDecisionInput's own comment.
    question: str
