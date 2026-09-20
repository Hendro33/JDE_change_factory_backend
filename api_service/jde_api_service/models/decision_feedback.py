"""
DecisionFeedback -- the human action feedback the design document's
Section 14.1 continuous-improvement loop needs captured "as it happens,
not reconstructed from memory later": rejections, Domain-Owner-edit
send-backs, and approvals, each tied to the change they're a reaction
to. This is additive, append-only telemetry alongside the authoritative
decision records that already exist (ApprovalRecord, DomainReview's own
approval fields, mcp_server's backlog.py/approval.py records) -- it
never replaces or duplicates their role as the actual gate, it only
keeps a queryable trail of the WHY behind each one, for future agent-
improvement analysis (Section 14.2). Nothing reads this to make a
decision; only Admin > Agents' health view reads it, to summarise.

Kinds are limited to the governance actions api_service actually
implements server-side today (domain-owner/application-manager
approval, a domain-owner edit send-back, and exact-change approval/
rejection) -- there is deliberately no "story_approval"/"story_
rejection" kind, because that action is not wired to any real backend
route today (see architecture_review.py / domain_governance.py).
"""

from __future__ import annotations

from typing import Literal, Optional

from .base import ApiModel

DecisionFeedbackKind = Literal[
    "domain_owner_approval",
    "domain_owner_edit",
    "application_manager_approval",
    "exact_change_approval",
    "exact_change_rejection",
]

FeedbackReasonCode = Literal[
    "missing_information",
    "wrong_business_domain",
    "incorrect_analysis_or_route",
    "risk_or_compliance_concern",
    "duplicate_or_superseded",
    "other",
]


class DecisionFeedback(ApiModel):
    id: str
    change_id: str
    customer_id: str
    kind: DecisionFeedbackKind
    decided_by: str
    identity_id: Optional[str] = None
    reason_code: Optional[FeedbackReasonCode] = None
    note: str = ""
    recorded_at: str
