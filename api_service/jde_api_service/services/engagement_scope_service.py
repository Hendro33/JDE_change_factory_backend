"""
Persistence for the per-customer EngagementScope -- see
models/engagement_scope.py for why this exists alongside, not instead
of, mcp_server's single global scope.json. JsonFileStore-backed like
every other api_service-owned collection, keyed by customer_id: one
document per customer, since the customer's whole scope IS the document
(unlike business_domains, where each domain is its own document).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ..models.engagement_scope import EngagementScope, EngagementScopeUpdate
from ..persistence.json_file_store import JsonFileStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EngagementScopeService:
    def __init__(self, directory: str) -> None:
        self._store = JsonFileStore(directory)

    def get_for_customer(self, customer_id: str) -> Optional[EngagementScope]:
        doc = self._store.get(customer_id)
        return EngagementScope.model_validate(doc) if doc else None

    def upsert(self, customer_id: str, payload: EngagementScopeUpdate) -> EngagementScope:
        scope = EngagementScope(
            customer_id=customer_id,
            tools_release=payload.tools_release,
            functional_agent=payload.functional_agent,
            technical_agent=payload.technical_agent,
            updated_at=_now(),
            updated_by=payload.updated_by,
        )
        self._store.put(customer_id, scope.model_dump(mode="json", by_alias=False))
        return scope
