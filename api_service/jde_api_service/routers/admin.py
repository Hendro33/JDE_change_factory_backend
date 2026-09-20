"""
Administration area -- read-mostly views over existing customer,
governance, engagement-scope, agent-definition and agent-run data, plus
the one genuinely new per-customer write surface this increment adds
(EngagementScope) and the Business Domain create/status actions the
service layer already supported but had no route for.

Deliberately NOT here, on purpose: any endpoint that would accept or
return an AIS/JDE credential (see AisConnectionStatus -- status only,
never a value); any endpoint that edits a .claude/agents/*.md file;
and any endpoint that widens what an agent's tool access is beyond
what its own .md file already declares.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from jde_mcp_server import config as mcp_config

from ..dependencies import AuthContext, require_customer_access
from ..models.admin import (
    AgentHealth,
    AgentRunSummary,
    AisConnectionStatus,
    CustomerProfile,
    ErpLandscape,
    FeedbackSummary,
    IdentitySummary,
    IntegrationStatus,
)
from ..models.agent_registry import AgentDefinition
from ..models.business_domain import BusinessDomain, BusinessDomainCreate, BusinessDomainStatusUpdate
from ..models.engagement_scope import EngagementScope, EngagementScopeUpdate
from ..models.session import Customer as CustomerOut
from ..services.customer_service import get_registry
from ..services.registry import (
    get_agent_registry_service,
    get_agent_run_service,
    get_business_domain_service,
    get_decision_feedback_service,
    get_engagement_scope_service,
)

router = APIRouter(prefix="/admin", tags=["admin"])


# ---------------------------------------------------------------------
# Customer Setup
# ---------------------------------------------------------------------
@router.get("/customer-profile", response_model=CustomerProfile)
def get_customer_profile(ctx: AuthContext = Depends(require_customer_access)) -> CustomerProfile:
    registry = get_registry()
    customer = registry.get_customer(ctx.customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail=f"no such customer: {ctx.customer_id}")
    identities = [
        IdentitySummary(id=i.id, display_name=i.display_name, role=i.role)
        for i in registry.identities_for_customer(ctx.customer_id)
    ]
    return CustomerProfile(
        customer=CustomerOut(
            id=customer.id,
            name=customer.name,
            short_name=customer.short_name,
            tools_release=customer.tools_release,
            environment=customer.environment,
        ),
        identities=identities,
    )


# ---------------------------------------------------------------------
# ERP / JDE Landscape
# ---------------------------------------------------------------------
_SCOPE_SHARED_NOTE = (
    "mcp_server's JDE (AIS) connection and its scope.json engagement file are still a single, global "
    "configuration shared by every customer in this deployment -- they are not yet customer-specific. The "
    "Engagement Scope below is this customer's own intended configuration; it is the source an operator would "
    "export into scope.json for this engagement, but it is not yet wired into mcp_server's live enforcement. "
    "Making the JDE connection and scope genuinely per-customer is a larger change, out of scope here."
)


@router.get("/erp-landscape", response_model=ErpLandscape)
def get_erp_landscape(ctx: AuthContext = Depends(require_customer_access)) -> ErpLandscape:
    registry = get_registry()
    customer = registry.get_customer(ctx.customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail=f"no such customer: {ctx.customer_id}")
    ais = mcp_config.settings
    scope = get_engagement_scope_service().get_for_customer(ctx.customer_id)
    configured = bool(
        scope
        and (
            scope.functional_agent.approved_versions
            or scope.technical_agent.authorized_object_types
        )
    )
    return ErpLandscape(
        customer_id=ctx.customer_id,
        tools_release=customer.tools_release,
        environment=customer.environment,
        ais=AisConnectionStatus(
            mock_mode=ais.mock_mode,
            base_url_configured=bool(ais.ais_base_url),
            environment=ais.ais_environment or None,
            role=ais.ais_role or None,
        ),
        engagement_scope_configured=configured,
        scope_globally_shared_note=_SCOPE_SHARED_NOTE,
    )


@router.get("/engagement-scope", response_model=EngagementScope)
def get_engagement_scope(ctx: AuthContext = Depends(require_customer_access)) -> EngagementScope:
    scope = get_engagement_scope_service().get_for_customer(ctx.customer_id)
    if scope is None:
        # Same "no configuration means no default permission" honesty
        # scope.py already applies -- an unconfigured scope is a real,
        # visible state, not silently defaulted to something permissive.
        return EngagementScope(customer_id=ctx.customer_id)
    return scope


@router.put("/engagement-scope", response_model=EngagementScope)
def update_engagement_scope(
    payload: EngagementScopeUpdate, ctx: AuthContext = Depends(require_customer_access)
) -> EngagementScope:
    return get_engagement_scope_service().upsert(ctx.customer_id, payload)


# ---------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------
# Which DecisionFeedback kinds are a genuine reaction to a specific
# agent's output. Deliberately partial: application-manager/domain-
# owner approvals are pure human governance steps, not a reaction to
# one agent's work, so they are not force-fit onto an agent's health
# card here -- they already have their own place in the Governance
# pages.
_AGENT_FEEDBACK_KINDS: dict[str, list[str]] = {
    "architect": ["exact_change_approval", "exact_change_rejection"],
    "improve-agent": ["domain_owner_edit"],
}


@router.get("/agents", response_model=list[AgentDefinition])
def list_agents(ctx: AuthContext = Depends(require_customer_access)) -> list[AgentDefinition]:
    return get_agent_registry_service().list_agents()


@router.get("/agents/{agent_name}", response_model=AgentDefinition)
def get_agent(agent_name: str, ctx: AuthContext = Depends(require_customer_access)) -> AgentDefinition:
    agent = get_agent_registry_service().get_agent(agent_name)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"no such agent: {agent_name}")
    return agent


@router.get("/agents/{agent_name}/health", response_model=AgentHealth)
def get_agent_health(agent_name: str, ctx: AuthContext = Depends(require_customer_access)) -> AgentHealth:
    # Run history is cross-customer by design (models/agent_run.py);
    # feedback below is filtered to the active customer.
    runs = get_agent_run_service().list_for_agent(agent_name, limit=20)
    run_counts: dict[str, int] = {}
    for r in runs:
        run_counts[r.stage] = run_counts.get(r.stage, 0) + 1

    feedback_summaries: list[FeedbackSummary] = []
    kinds = _AGENT_FEEDBACK_KINDS.get(agent_name, [])
    if kinds:
        customer_feedback = get_decision_feedback_service().list_for_customer(ctx.customer_id)
        for kind in kinds:
            matching = [f for f in customer_feedback if f.kind == kind]
            if not matching:
                continue
            reasons: dict[str, int] = {}
            for f in matching:
                if f.reason_code:
                    reasons[f.reason_code] = reasons.get(f.reason_code, 0) + 1
            feedback_summaries.append(FeedbackSummary(kind=kind, count=len(matching), reasons=reasons))

    return AgentHealth(
        agent_name=agent_name,
        recent_runs=[
            AgentRunSummary(
                run_id=r.run_id, story_id=r.story_id, stage=r.stage,
                started_at=r.started_at, updated_at=r.updated_at, error=r.error,
            )
            for r in runs
        ],
        run_counts=run_counts,
        feedback=feedback_summaries,
    )


# ---------------------------------------------------------------------
# Business Domains (write path -- the read path is GET /business-domains,
# routers/domain_governance.py, unchanged)
# ---------------------------------------------------------------------
@router.post("/business-domains", response_model=BusinessDomain, status_code=201)
def create_business_domain(
    payload: BusinessDomainCreate, ctx: AuthContext = Depends(require_customer_access)
) -> BusinessDomain:
    return get_business_domain_service().create(payload, ctx.customer_id)


@router.put("/business-domains/{domain_id}/status", response_model=BusinessDomain)
def update_business_domain_status(
    domain_id: str, payload: BusinessDomainStatusUpdate, ctx: AuthContext = Depends(require_customer_access)
) -> BusinessDomain:
    domain = get_business_domain_service().get_for_customer(domain_id, ctx.customer_id)
    if domain is None:
        raise HTTPException(status_code=404, detail=f"no such business domain: {domain_id}")
    return get_business_domain_service().update_status(domain_id, payload.status)


# ---------------------------------------------------------------------
# Integrations
# ---------------------------------------------------------------------
@router.get("/integrations", response_model=list[IntegrationStatus])
def list_integrations(ctx: AuthContext = Depends(require_customer_access)) -> list[IntegrationStatus]:
    ais = mcp_config.settings
    ais_live = (not ais.mock_mode) and bool(ais.ais_base_url)
    return [
        IntegrationStatus(
            name="JD Edwards (AIS)",
            connected=ais_live,
            detail=(
                "Live AIS connection configured" if ais_live
                else "Running in mock mode -- see ERP / JDE Landscape for connection status"
            ),
        ),
        IntegrationStatus(
            name="Topdesk",
            connected=False,
            detail="Not connected -- source and source reference are free-text fields today, no live connector",
        ),
        IntegrationStatus(
            name="Slack / Teams approvals",
            connected=False,
            detail="Not connected -- approvals happen in-app today",
        ),
    ]
