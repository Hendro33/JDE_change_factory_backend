"""
Change/UserStory/Evidence wire models -- mirrors src/types/domain.ts.

Every field the pipeline hasn't actually produced yet (architectDecision,
exactChange, testResult, humanValidation, closure) is Optional and left
None -- this assembler never fabricates a stage that hasn't happened.
"""

from __future__ import annotations

from typing import Literal, Optional

from .base import ApiModel

ChangeSource = Literal["Business", "Support / Topdesk", "Optimisation", "DevOps"]

LifecycleState = Literal[
    "RECEIVED", "REFINING", "BACKLOG_READY", "APPROVED", "REJECTED",
    "RESOLVED_WITHOUT_CHANGE", "ARCHITECTING", "SPEC_READY", "CHANGE_APPROVED",
    "EXECUTING", "TESTING", "VALIDATED", "CNC_HANDOFF", "CLOSED", "FAILED",
]

Priority = Literal["High", "Medium", "Low"]
Complexity = Literal["Low", "Medium", "High", "Unknown"]

ImplementationRoute = Literal[
    "Functional Agent", "Technical Agent", "Human Implementation", "Resolve without Change"
]
ChangeType = Literal["Configuration", "Functional Change", "Technical Change", "Investigation", "Other"]


class BusinessImpact(ApiModel):
    financial_impact: str = ""
    operational_reach: str = ""
    risk_compliance: str = ""
    strategic_alignment: str = ""
    urgency: str = ""


class AcceptanceCriterion(ApiModel):
    id: str
    text: str
    verified_by: Optional[str] = None


class TestStep(ApiModel):
    id: str
    action: str
    expected: str


class UserStory(ApiModel):
    statement: str
    business_context: str = ""
    acceptance_criteria: list[AcceptanceCriterion] = []
    test_script: list[TestStep] = []
    open_questions: list[str] = []
    quality_status: Literal["draft", "needs_revision", "passed", "needs_human_input"] = "passed"
    revision_count: int = 0


class ArchitectDecision(ApiModel):
    recommended_route: ImplementationRoute
    confidence: float
    existing_functionality_found: str = ""
    alternatives_considered: list[dict] = []
    objects_affected: list[str] = []
    dependencies_and_conflicts: list[str] = []
    rollback_strategy: str = ""
    decided_at: str


class ImplementationSpecification(ApiModel):
    sequence: list[str] = []
    required_mcp_operations: list[str] = []
    human_actions_required: list[str] = []
    validation_approach: str = ""


class ExactChange(ApiModel):
    tool: str
    application: str
    version: str
    option: str
    current_value: str = ""
    proposed_value: str = ""
    environment: str = "DEV"
    test_orchestration: str = ""


class ApprovalRecord(ApiModel):
    approval_id: str
    kind: Literal["story", "change", "domain_owner", "application_manager"]
    status: Literal["pending", "approved", "rejected"]
    change_hash: Optional[str] = None
    approved_by: Optional[str] = None
    approved_at: Optional[str] = None
    expires_at: Optional[str] = None
    note: Optional[str] = None


class TestSpecification(ApiModel):
    mode: Literal["JDE automated", "Integration automated", "Human executable", "Mixed", "Not automatable"]
    orchestration_name: Optional[str] = None
    steps: list[TestStep] = []


class TestResult(ApiModel):
    outcome: Literal["pass", "fail", "not run"] = "not run"
    ran_at: Optional[str] = None
    detail: Optional[str] = None


class EvidenceRecord(ApiModel):
    entry_id: str
    stage: str
    detail: str
    actor: str
    agent_versions: Optional[dict[str, str]] = None
    captured_at: str
    prev_hash: str
    entry_hash: str


class ClosureRecord(ApiModel):
    what_changed: str
    business_facing_result: str
    limitations: str = ""
    source_update_status: Literal["written back", "hand-off produced", "not applicable"]
    requester_confirmation: Literal["solved", "not solved", "awaiting response"]
    closed_at: Optional[str] = None


class Change(ApiModel):
    id: str
    customer_id: str
    title: str
    source: ChangeSource
    source_reference: str = ""
    original_request: str = ""
    change_type: ChangeType = "Other"
    state: LifecycleState
    priority: Priority = "Medium"
    complexity_signal: Complexity = "Unknown"
    business_impact: BusinessImpact = BusinessImpact()
    created_at: str
    updated_at: str
    updated_by: str = ""

    # Live status of an in-flight Receive/Improve/Check run (Section
    # 12.1's driver), presentational only -- None once there is no run
    # associated with this change, or once it has finished and been
    # folded into user_story/state below.
    processing_stage: Optional[Literal["receiving", "improving", "checking", "done", "failed"]] = None
    processing_error: Optional[str] = None

    # Business domain governance (Increment: domain ownership). Both are
    # a read-only projection of the DomainReview sidecar (domain_review.py)
    # for list/filter display -- the full record (history, notes,
    # approvals) is fetched separately via GET /changes/{id}/domain-review.
    business_domain_id: Optional[str] = None
    domain_review_stage: Optional[
        Literal[
            "ready_for_domain_owner", "domain_owner_reviewing", "domain_owner_requested_revision",
            "reviewer_agent_refining", "domain_owner_approved", "ready_for_application_manager",
            "application_manager_approved",
        ]
    ] = None

    user_story: Optional[UserStory] = None
    story_approval: Optional[ApprovalRecord] = None
    architect_decision: Optional[ArchitectDecision] = None
    implementation_spec: Optional[ImplementationSpecification] = None
    exact_change: Optional[ExactChange] = None
    change_approval: Optional[ApprovalRecord] = None
    test_specification: Optional[TestSpecification] = None
    test_result: Optional[TestResult] = None
    human_validation: Optional[dict] = None
    evidence: list[EvidenceRecord] = []
    closure: Optional[ClosureRecord] = None
