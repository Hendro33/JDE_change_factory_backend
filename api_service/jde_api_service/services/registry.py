"""
Wiring for this service's own services. Kept in one small module so
routers depend on functions here rather than constructing services
themselves -- this is the "service modules underneath the API, not
business logic in route handlers" boundary in practice.

Deliberately NOT cached as module-level singletons: each of these is
just a thin wrapper around a directory path (construction is cheap),
and building fresh per call means tests can point settings.data_dir at
a temp directory without fighting stale global state.
"""

from __future__ import annotations

import os

from ..config import settings
from .agent_registry_service import AgentRegistryService
from .agent_run_service import AgentRunService
from .architecture_review_service import ArchitectureReviewService
from .business_domain_service import BusinessDomainService
from .capability_service import CapabilityService
from .change_request_service import ChangeRequestService
from .change_service import ChangeService
from .customer_link_service import CustomerLinkService
from .decision_feedback_service import DecisionFeedbackService
from .delivery_queue_service import DeliveryQueueService
from .domain_review_service import DomainReviewService
from .engagement_scope_service import EngagementScopeService
from .enhancement_run_service import EnhancementRunService
from .jira_credentials_service import JiraCredentialsService
from .jira_gateway import JiraGateway, JiraHttpGateway, JiraMockGateway
from .jira_integration_service import JiraIntegrationService
from .jira_sync_service import JiraSyncService
from .metrics_service import MetricsService


def get_change_request_service() -> ChangeRequestService:
    return ChangeRequestService(os.path.join(settings.data_dir, "change_requests"))


def get_customer_link_service() -> CustomerLinkService:
    return CustomerLinkService(os.path.join(settings.data_dir, "customer_links"))


def get_enhancement_run_service() -> EnhancementRunService:
    return EnhancementRunService(os.path.join(settings.data_dir, "enhancement_runs"))


def get_business_domain_service() -> BusinessDomainService:
    return BusinessDomainService(os.path.join(settings.data_dir, "business_domains"))


def get_domain_review_service() -> DomainReviewService:
    return DomainReviewService(os.path.join(settings.data_dir, "domain_reviews"))


def get_delivery_queue_service() -> DeliveryQueueService:
    return DeliveryQueueService(os.path.join(settings.data_dir, "delivery_queue"))


def get_architecture_review_service() -> ArchitectureReviewService:
    return ArchitectureReviewService(os.path.join(settings.data_dir, "architecture_reviews"))


def get_engagement_scope_service() -> EngagementScopeService:
    return EngagementScopeService(os.path.join(settings.data_dir, "engagement_scope"))


def get_decision_feedback_service() -> DecisionFeedbackService:
    return DecisionFeedbackService(os.path.join(settings.data_dir, "decision_feedback"))


def get_agent_run_service() -> AgentRunService:
    return AgentRunService(os.path.join(settings.data_dir, "agent_runs"))


def get_agent_registry_service() -> AgentRegistryService:
    return AgentRegistryService(settings.repo_root)


def get_capability_service() -> CapabilityService:
    return CapabilityService()


def get_change_service() -> ChangeService:
    return ChangeService(
        get_change_request_service(),
        get_customer_link_service(),
        get_enhancement_run_service(),
        get_domain_review_service(),
        get_architecture_review_service(),
    )


def get_metrics_service() -> MetricsService:
    return MetricsService(get_change_service(), get_business_domain_service(), get_delivery_queue_service())


def get_jira_integration_service() -> JiraIntegrationService:
    # SQLite-backed (persistence/db.py), not a directory -- see that
    # service's own docstring for why this moved off JsonFileStore.
    return JiraIntegrationService()


def get_jira_credentials_service() -> JiraCredentialsService:
    return JiraCredentialsService()


class JiraUnavailable(RuntimeError):
    """Real mode, but this company's Jira cannot be used: nothing is
    fetched, nothing is written, and nothing is quietly mocked."""


def jira_mode(customer_id: str) -> tuple[str, str]:
    """(mode, reason) for one company's Jira connector.

    demo        -- only when the deployment explicitly runs Jira in demo
                   mode (JDE_JIRA_MOCK_MODE=true); the mock gateway is used.
    live        -- a readable credential and a complete configuration.
    unavailable -- anything else, with the reason. There is no automatic
                   fallback to the mock: a real-mode deployment with a
                   missing, unreadable or incomplete setup says so and
                   blocks every Jira operation."""
    if settings.jira_mock_mode:
        return "demo", "This deployment runs Jira in demo mode (JDE_JIRA_MOCK_MODE=true); nothing reaches a real Jira."
    credentials = get_jira_credentials_service()
    storage = credentials.storage_status(customer_id)
    if storage == "none":
        return "unavailable", "No Jira credential is saved for this company. An Admin must enter it under Admin > Integrations > Jira."
    if storage == "unreadable":
        return "unavailable", (
            "The saved Jira token cannot be decrypted with this server's key. Restore the matching key, "
            "or have an Admin re-enter the token."
        )
    if not credentials.is_configured(customer_id):
        return "unavailable", "The saved Jira credential is incomplete (email or token missing)."
    config = get_jira_integration_service().get_for_customer(customer_id)
    if config is None or not config.is_configured():
        return "unavailable", "The Jira site, project or status configuration is not complete."
    return "live", ""


def jira_is_live_for_customer(customer_id: str) -> bool:
    return jira_mode(customer_id)[0] == "live"


def get_jira_gateway(customer_id: str) -> JiraGateway:
    mode, reason = jira_mode(customer_id)
    if mode == "demo":
        return JiraMockGateway()
    if mode == "unavailable":
        raise JiraUnavailable(reason)
    creds = get_jira_credentials_service().get_for_customer(customer_id)
    return JiraHttpGateway(email=creds.email, api_token=creds.api_token)


def get_jira_sync_service(customer_id: str) -> JiraSyncService:
    return JiraSyncService(get_jira_integration_service(), get_change_request_service(), get_jira_gateway(customer_id))
