"""
Persistence for per-customer Jira credentials (email + API token).

PILOT-SCOPED simplification, deliberately: plain JsonFileStore, the
same file-per-document pattern every other api_service-owned collection
already uses (jira_integration_service.py, domain_review_service.py,
...) -- not a secrets manager, not encrypted at rest. The directory
this writes to sits under settings.data_dir (./api_data by default),
which is git-ignored (see .gitignore's "api_data/" entry), so these
documents are never committed. See models/jira_integration.py's own
docstring for the full pilot/production distinction this follows, and
docs/JDE_AI_Driven_Change_Factory_Design_Document_v11.docx Section
19.7 for where it's now recorded as explicit future hardening.

The token never leaves this service as part of a router response --
see JiraCredentials' own docstring: it must never be a FastAPI
response_model.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ..models.jira_integration import JiraCredentials, JiraCredentialsUpdate
from ..persistence.json_file_store import JsonFileStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JiraCredentialsService:
    def __init__(self, directory: str) -> None:
        self._store = JsonFileStore(directory)

    def get_for_customer(self, customer_id: str) -> Optional[JiraCredentials]:
        doc = self._store.get(customer_id)
        return JiraCredentials.model_validate(doc) if doc else None

    def is_configured(self, customer_id: str) -> bool:
        creds = self.get_for_customer(customer_id)
        return bool(creds and creds.email and creds.api_token)

    def upsert(self, customer_id: str, payload: JiraCredentialsUpdate) -> JiraCredentials:
        creds = JiraCredentials(
            customer_id=customer_id,
            email=payload.email,
            api_token=payload.api_token,
            updated_at=_now(),
            updated_by=payload.updated_by,
        )
        self._store.put(customer_id, creds.model_dump(mode="json", by_alias=False))
        return creds

    def delete(self, customer_id: str) -> None:
        """Disconnect -- removes this customer's stored credential
        entirely (not just blanking the fields), so jira_is_live_for_customer
        goes back to false immediately. Idempotent, same as
        JsonFileStore.delete."""
        self._store.delete(customer_id)
