"""
EngagementScope -- the per-company, Admin-saved configuration that
answers "what is this engagement actually authorised to touch in JDE,
and who may approve it."

This record IS what the execution gate enforces: mcp_server's scope.py
reads the stored file for the story's own company (JDE_COMPANY_SCOPE_DIR,
wired up in main.py). The stored document is snake_case and that file
format is the contract between the two packages. Missing sections
authorise nothing; a missing or unreadable approval policy blocks both
approval and execution.

Two sections are reference only and are not read by the gate:
functional_agent.never_touch_categories and the free-text approvers
lists. Approval authority comes from company roles plus approval_policy.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from jde_mcp_server import capability_catalog
from pydantic import Field, field_validator, model_validator

from .base import ApiModel

Mechanism = Literal["ais_form_service_request", "ais_orchestration"]
TestSideEffect = Literal["none", "creates_dev_transaction", "posting", "payment", "outbound_integration", "batch_run"]


def _known_category(value: str) -> str:
    value = value.strip()
    if value and value not in capability_catalog.known_option_categories():
        raise ValueError(
            f"unknown option category {value!r}; use one of {sorted(capability_catalog.known_option_categories())}"
        )
    return value


class ApprovedVersion(ApiModel):
    # Which capability_catalog.json entry this target is enabled for. The
    # execution gate refuses an entry with no capability_id.
    capability_id: str = ""
    # What kind of option this is, from the capability's closed list
    # (capability_catalog.json enforcement.option_categories). Declared by
    # the Admin who saves it; the gate refuses an undeclared, protected or
    # never-touch category.
    option_category: str = ""
    application: str
    version: str
    options: list[str] = []
    allowed_values: list[str] = []
    notes: str = ""

    @field_validator("option_category")
    @classmethod
    def _category(cls, value: str) -> str:
        return _known_category(value)


class SpikeExperiment(ApiModel):
    """A bounded DEV validation experiment for a Needs-spike capability.
    Only valid until expires_at; a missing or past expiry blocks it."""

    capability_id: str
    capability_revision: str
    application: str
    version: str
    option: str = ""
    environment: str = "DEV"
    expires_at: str  # ISO-8601 with timezone
    note: str = ""
    # Stamped server-side from the authenticated session when saved.
    approved_by: Optional[str] = None
    approved_at: Optional[str] = None

    @field_validator("expires_at")
    @classmethod
    def _dated(cls, value: str) -> str:
        # The gate treats an unreadable or zone-less expiry as no expiry
        # (allows nothing); refusing it here makes that visible on save.
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("expires_at must be an ISO-8601 date-time")
        if parsed.tzinfo is None:
            raise ValueError("expires_at must include a timezone, e.g. 2026-10-31T17:00:00+01:00")
        return value.strip()


class FunctionalAgentScope(ApiModel):
    approved_versions: list[ApprovedVersion] = []
    spike_experiments: list[SpikeExperiment] = []
    # ENFORCED: option categories (closed list) this company never lets
    # Jade write, on top of those the capability itself protects.
    never_touch_categories: list[str] = []
    # Reference only, not read by the gate: free-text notes. Earlier
    # builds stored free text in never_touch_categories; on load, any
    # value that is not a known category is moved here, visibly, rather
    # than silently treated as enforced.
    never_touch_notes: list[str] = []
    approvers: list[str] = []

    @model_validator(mode="before")
    @classmethod
    def _split_free_text(cls, data):
        if isinstance(data, dict):
            key = "never_touch_categories" if "never_touch_categories" in data else "neverTouchCategories"
            values = data.get(key) or []
            known = capability_catalog.known_option_categories()
            notes_key = "never_touch_notes" if key == "never_touch_categories" else "neverTouchNotes"
            data = {**data, key: [v for v in values if v in known],
                    notes_key: [*(data.get(notes_key) or []), *[v for v in values if v not in known]]}
        return data


class ApprovedTest(ApiModel):
    """A test the company allows Jade to run in DEV, with the side effects
    it has, from a closed list. The gate refuses a test that is not listed
    here, declares nothing, or declares a side effect the capability does
    not permit (posting, payments, outbound integrations, batch runs)."""

    orchestration: str
    side_effects: list[TestSideEffect] = Field(min_length=1)
    note: str = ""


class TestScope(ApiModel):
    approved_tests: list[ApprovedTest] = []


class TechnicalAgentScope(ApiModel):
    authorized_object_types: list[str] = []
    reserved_product_code: str = ""
    naming_prefix: str = ""
    # Reference only -- not read by the execution gate.
    approvers: list[str] = []


class EnvironmentBinding(ApiModel):
    """The one DEV environment this company's executions may target.
    Isolation is a human (CNC) confirmation recorded with evidence; the
    gate refuses to run until it is confirmed."""

    dev_environment_id: str = ""
    dev_path_code: str = ""
    ais_data_source_name: str = ""
    isolation_confirmed: bool = False
    isolation_evidence: str = ""
    # Stamped server-side when isolation_confirmed is first set true.
    isolation_confirmed_by: Optional[str] = None
    isolation_confirmed_at: Optional[str] = None


ApproverRole = Literal["admin", "product_manager", "domain_owner"]


class ApprovalPolicy(ApiModel):
    """Who may approve an exact change for this company, and for how
    long that approval stays valid. The gate (scope.require_approval_policy)
    refuses any other version or field -- keep the two in step."""

    policy_version: Literal[1] = 1
    exact_change_approver_roles: list[ApproverRole] = Field(min_length=1)
    approval_valid_hours: int = Field(default=24, ge=1, le=168)


class EngagementScope(ApiModel):
    customer_id: str
    tools_release: str = ""
    environment: EnvironmentBinding = EnvironmentBinding()
    functional_agent: FunctionalAgentScope = FunctionalAgentScope()
    technical_agent: TechnicalAgentScope = TechnicalAgentScope()
    # None = no policy: nobody can approve an exact change, nothing executes.
    approval_policy: Optional[ApprovalPolicy] = None
    # ENFORCED: the execution mechanisms this company allows. Empty allows none.
    mechanisms_allowed: list[Mechanism] = []
    test_scope: TestScope = TestScope()
    # 0 = never saved. Incremented on every save; the execution gate
    # stamps it onto change records as the scope revision used.
    revision: int = 0
    # None means "never configured" -- distinct from an explicitly
    # empty-but-saved scope, same honesty this whole model exists for.
    updated_at: Optional[str] = None
    # Always the authenticated user who saved -- never client-supplied.
    updated_by: Optional[str] = None


class EngagementScopeUpdate(ApiModel):
    """Full-replace payload -- an admin edits the whole scope at once,
    same convention as the frontend's other edit forms. No partial-patch
    semantics, to keep this simple. Any client-sent updatedBy is ignored
    (extra fields are dropped); the actor comes from the session."""

    tools_release: str = ""
    environment: EnvironmentBinding = EnvironmentBinding()
    functional_agent: FunctionalAgentScope = FunctionalAgentScope()
    technical_agent: TechnicalAgentScope = TechnicalAgentScope()
    approval_policy: Optional[ApprovalPolicy] = None
    mechanisms_allowed: list[Mechanism] = []
    test_scope: TestScope = TestScope()
    # The revision the client loaded; required once the scope exists.
    expected_revision: Optional[int] = None
