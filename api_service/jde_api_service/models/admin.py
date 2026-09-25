"""
Read-model shapes for the Admin area's Customer Setup, ERP Landscape,
Agents (health) and Integrations screens. Every field here is a
projection of an EXISTING record (Customer, Identity, mcp_server's own
AIS config, AgentRun, DecisionFeedback) -- nothing here is a new source
of truth.
"""

from __future__ import annotations

from typing import Optional

from .base import ApiModel
from .session import Customer


class IdentitySummary(ApiModel):
    id: str
    display_name: str
    role: str


class CustomerProfile(ApiModel):
    customer: Customer
    identities: list[IdentitySummary] = []


class AisConnectionStatus(ApiModel):
    """Status only -- NEVER a credential. Reflects mcp_server's own
    process-wide config (Section-honest note: this is one global AIS
    connection today, shared by every customer, not yet per-customer)."""

    mock_mode: bool
    base_url_configured: bool
    environment: Optional[str] = None
    role: Optional[str] = None


class DiscoveryProfileSummary(ApiModel):
    """A read-only reference to the authoritative JDE discovery profile
    (Admin > Integrations > JDE). ERP Landscape never stores or edits
    connection settings itself."""

    configured: bool
    revision: int = 0
    connection_name: Optional[str] = None
    environment: Optional[str] = None
    environment_purpose: Optional[str] = None
    path_code: Optional[str] = None
    application_release: Optional[str] = None
    tools_release: Optional[str] = None
    mode: Optional[str] = None
    discovery_enabled: bool = False
    disabled: bool = False
    health: dict[str, str] = {}


class ErpLandscape(ApiModel):
    customer_id: str
    tools_release: str
    environment: str
    # The EXECUTION gate's connection (process-wide, separate from discovery).
    ais: AisConnectionStatus
    discovery_profile: Optional[DiscoveryProfileSummary] = None
    engagement_scope_configured: bool
    # Deliberately always present and always the same text: this is a
    # structural fact about the current architecture, not a per-call
    # computed warning that could be silently dropped later.
    scope_globally_shared_note: str


class IntegrationStatus(ApiModel):
    name: str
    connected: bool
    detail: str


class AgentRunSummary(ApiModel):
    run_id: str
    story_id: str
    stage: str
    started_at: str
    updated_at: str
    error: Optional[str] = None


class FeedbackSummary(ApiModel):
    kind: str
    count: int
    reasons: dict[str, int] = {}


class AgentHealth(ApiModel):
    agent_name: str
    # Cross-customer -- see models/agent_run.py's own note on why.
    recent_runs: list[AgentRunSummary] = []
    run_counts: dict[str, int] = {}
    # Customer-scoped (the active customer's own decision feedback only).
    feedback: list[FeedbackSummary] = []
