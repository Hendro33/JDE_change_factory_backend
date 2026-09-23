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

from pydantic import Field, field_validator

from .base import ApiModel


class ApprovedVersion(ApiModel):
    # Which capability_catalog.json entry this target is enabled for. The
    # execution gate refuses an entry with no capability_id.
    capability_id: str = ""
    application: str
    version: str
    options: list[str] = []
    allowed_values: list[str] = []
    notes: str = ""


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
    # Reference only -- recorded for humans, NOT read by the execution
    # gate (which enforces approved_versions and the catalogue's own
    # restrictions). Kept because Appendix D asks for them.
    never_touch_categories: list[str] = []
    approvers: list[str] = []


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
    # The revision the client loaded; required once the scope exists.
    expected_revision: Optional[int] = None
