"""
EngagementScope -- the per-customer, human-authored configuration that
answers "what is this engagement actually authorised to touch in JDE."

This is api_service's own, customer-scoped counterpart to the single
global scope.json the design document's Appendix D.2/E.2 describes and
mcp_server/jde_mcp_server/scope.py enforces. It does NOT replace
scope.json and does NOT change how mcp_server enforces engagement scope
-- that module stays exactly as it is (sibling package, unmodified).
Today there is exactly one scope.json for the whole deployment, keyed
by nothing; this model is the intended per-customer source of truth an
operator would export into that file per engagement, once the MCP/AIS
layer itself becomes multi-tenant -- a larger, explicitly out-of-scope
change this increment does not attempt (see the ERP Landscape screen's
own honesty note, surfaced via models/admin.py).

Same "no configuration means no default permission" principle scope.py
already applies: an EngagementScope with no approved_versions/
authorized_object_types authorises nothing, and is shown as such rather
than silently defaulted to something permissive.
"""

from __future__ import annotations

from typing import Optional

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


class EngagementScope(ApiModel):
    customer_id: str
    tools_release: str = ""
    environment: EnvironmentBinding = EnvironmentBinding()
    functional_agent: FunctionalAgentScope = FunctionalAgentScope()
    technical_agent: TechnicalAgentScope = TechnicalAgentScope()
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
    # The revision the client loaded; required once the scope exists.
    expected_revision: Optional[int] = None
