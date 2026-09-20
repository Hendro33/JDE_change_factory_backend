"""
Persistence for the per-customer JiraIntegrationConfig -- see
models/jira_integration.py for why this is customer-scoped and why the
API token is never part of what this module stores. JsonFileStore-backed
like every other api_service-owned collection, keyed by customer_id: one
document per customer, same convention EngagementScopeService already
uses for its own per-customer document.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ..models.jira_integration import JiraIntegrationConfig, JiraIntegrationConfigUpdate
from ..persistence.json_file_store import JsonFileStore
from .jira_gateway import normalize_jira_base_url


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JiraIntegrationService:
    def __init__(self, directory: str) -> None:
        self._store = JsonFileStore(directory)

    def get_for_customer(self, customer_id: str) -> Optional[JiraIntegrationConfig]:
        doc = self._store.get(customer_id)
        return JiraIntegrationConfig.model_validate(doc) if doc else None

    def upsert(self, customer_id: str, payload: JiraIntegrationConfigUpdate) -> JiraIntegrationConfig:
        """Raises jira_gateway.InvalidJiraBaseUrl (a ValueError) if
        base_url isn't a bare site URL -- e.g. a project/queue/issue
        link pasted in by mistake. The router turns that into a 422."""
        config = JiraIntegrationConfig(
            customer_id=customer_id,
            base_url=normalize_jira_base_url(payload.base_url),
            project_key=payload.project_key,
            pickup_status=payload.pickup_status,
            post_pickup_status=payload.post_pickup_status,
            jade_id_field=payload.jade_id_field,
            request_type_field=payload.request_type_field,
            updated_at=_now(),
            updated_by=payload.updated_by,
        )
        self._store.put(customer_id, config.model_dump(mode="json", by_alias=False))
        return config
