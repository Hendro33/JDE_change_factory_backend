"""
Administration area -- read-mostly views over existing customer,
governance, engagement-scope, agent-definition and agent-run data, plus
the one genuinely new per-customer write surface this increment adds
(EngagementScope) and the Business Domain create/status actions the
service layer already supported but had no route for.

Deliberately NOT here, on purpose: any endpoint that would accept or
return an AIS/JDE credential (see AisConnectionStatus -- status only,
never a value); any endpoint that edits a .claude/agents/*.md file; and
any endpoint that widens what an agent's tool access is beyond what its
own .md file already declares.

The Jira credential is the one deliberate, explicitly pilot-scoped
exception to "no credential through the Admin API": update_jira_credentials
below accepts an email + API token so an admin can configure and test a
real Jira connection without editing backend files, and test_jira_connection
makes an ad-hoc live check of whatever is currently typed. Both are
write-only from the API's own point of view -- the token is never
echoed back by any endpoint (JiraConnectionStatus/JiraTestConnectionResult
report status/outcome only), never logged, and is persisted through
plain JsonFileStore under the git-ignored data directory -- not a
secrets manager. See models/jira_integration.py's own docstring and
docs/JDE_AI_Driven_Change_Factory_Design_Document_v11.docx Section 19.7
for the explicit pilot/production distinction this follows: production
credential entry belongs behind a real secrets provider, which this
pilot deliberately does not build.
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
from ..config import settings as api_settings
from ..models.agent_registry import AgentDefinition
from ..models.business_domain import BusinessDomain, BusinessDomainCreate, BusinessDomainStatusUpdate
from ..models.engagement_scope import EngagementScope, EngagementScopeUpdate
from ..models.jira_integration import (
    JiraConnectionStatus,
    JiraCredentialsUpdate,
    JiraIntegrationConfig,
    JiraIntegrationConfigUpdate,
    JiraSyncResult,
    JiraTestConnectionInput,
    JiraTestConnectionResult,
)
from ..models.session import Customer as CustomerOut
from ..services.customer_service import get_registry
from ..services.jira_gateway import test_live_connection
from ..services.jira_sync_service import JiraNotConfigured
from ..services.registry import (
    get_agent_registry_service,
    get_agent_run_service,
    get_business_domain_service,
    get_decision_feedback_service,
    get_engagement_scope_service,
    get_jira_credentials_service,
    get_jira_integration_service,
    get_jira_sync_service,
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
    "improve-agent": ["domain_owner_edit", "domain_owner_rejection"],
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

    jira_config = get_jira_integration_service().get_for_customer(ctx.customer_id)
    jira_credentials_ok = get_jira_credentials_service().is_configured(ctx.customer_id)
    jira_live = (not api_settings.jira_mock_mode) and jira_credentials_ok and bool(jira_config and jira_config.is_configured())
    if api_settings.jira_mock_mode:
        jira_detail = "Running in mock mode -- see Jira below to configure and try a sync"
    elif not jira_config or not jira_config.is_configured():
        jira_detail = "Not configured for this customer -- see Jira below"
    elif not jira_credentials_ok:
        jira_detail = "Customer configuration is set, but no Jira credential is configured for this customer -- see Jira below"
    else:
        jira_detail = f"Connected to project {jira_config.project_key}"

    return [
        IntegrationStatus(
            name="JD Edwards (AIS)",
            connected=ais_live,
            detail=(
                "Live AIS connection configured" if ais_live
                else "Running in mock mode -- see ERP / JDE Landscape for connection status"
            ),
        ),
        IntegrationStatus(name="Jira Service Management", connected=jira_live, detail=jira_detail),
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


# ---------------------------------------------------------------------
# Jira Service Management hand-off (jira_gateway.py / jira_sync_service.py)
# ---------------------------------------------------------------------
@router.get("/jira-integration", response_model=JiraIntegrationConfig)
def get_jira_integration(ctx: AuthContext = Depends(require_customer_access)) -> JiraIntegrationConfig:
    config = get_jira_integration_service().get_for_customer(ctx.customer_id)
    if config is None:
        # Same "no configuration means not configured" honesty as
        # get_engagement_scope -- never a silently defaulted value.
        return JiraIntegrationConfig(customer_id=ctx.customer_id)
    return config


@router.put("/jira-integration", response_model=JiraIntegrationConfig)
def update_jira_integration(
    payload: JiraIntegrationConfigUpdate, ctx: AuthContext = Depends(require_customer_access)
) -> JiraIntegrationConfig:
    return get_jira_integration_service().upsert(ctx.customer_id, payload)


@router.get("/jira-integration/status", response_model=JiraConnectionStatus)
def get_jira_integration_status(ctx: AuthContext = Depends(require_customer_access)) -> JiraConnectionStatus:
    config = get_jira_integration_service().get_for_customer(ctx.customer_id)
    return JiraConnectionStatus(
        mock_mode=api_settings.jira_mock_mode,
        credentials_configured=get_jira_credentials_service().is_configured(ctx.customer_id),
        config_configured=bool(config and config.is_configured()),
    )


@router.put("/jira-credentials", response_model=JiraConnectionStatus)
def update_jira_credentials(
    payload: JiraCredentialsUpdate, ctx: AuthContext = Depends(require_customer_access)
) -> JiraConnectionStatus:
    """Enters or replaces this customer's Jira email + API token --
    the one deliberate exception to "no credential through the Admin
    API" (see this router's own docstring). The token is accepted here
    and never echoed back by this or any other endpoint: the response
    is status only, exactly like get_jira_integration_status above."""
    get_jira_credentials_service().upsert(ctx.customer_id, payload)
    config = get_jira_integration_service().get_for_customer(ctx.customer_id)
    return JiraConnectionStatus(
        mock_mode=api_settings.jira_mock_mode,
        credentials_configured=True,
        config_configured=bool(config and config.is_configured()),
    )


@router.post("/jira-integration/test-connection", response_model=JiraTestConnectionResult)
def test_jira_connection(
    payload: JiraTestConnectionInput, ctx: AuthContext = Depends(require_customer_access)
) -> JiraTestConnectionResult:
    """"Test Connection" -- checks whatever is currently typed in the
    Jira form, whether or not it has been saved yet, and never
    persists it. Always makes a real call to Jira regardless of
    JDE_JIRA_MOCK_MODE: that flag governs the sync pipeline, not this
    button, whose entire purpose is verifying the real connection."""
    ok, message = test_live_connection(
        base_url=payload.base_url, email=payload.email, api_token=payload.api_token, project_key=payload.project_key,
    )
    return JiraTestConnectionResult(ok=ok, message=message)


@router.post("/jira-integration/sync", response_model=JiraSyncResult)
def sync_jira_integration(ctx: AuthContext = Depends(require_customer_access)) -> JiraSyncResult:
    try:
        return get_jira_sync_service(ctx.customer_id).sync_for_customer(ctx.customer_id)
    except JiraNotConfigured as exc:
        raise HTTPException(status_code=409, detail=str(exc))
