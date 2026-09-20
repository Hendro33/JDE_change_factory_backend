"""
ChangeRequest persistence.

One store, multiple ways to write into it -- create_direct (a person
using the "New change request" form), create_from_topdesk (the mock
Topdesk connector's normalized output) and create_from_jira (the Jira
sync service's normalized output) all converge on the same _create()
and the same JsonFileStore. There is no separate ticket database and no
separate workflow per source; a Jira- or Topdesk-sourced record is a
ChangeRequest exactly like a direct one, distinguished only by its
sourceType field.

A ChangeRequest is customer-scoped from the moment it's created (the
customer_id comes from the already-validated X-Customer-Id for real
intake, or is supplied directly by the pilot-data seeder / the Jira
sync service acting on a customer's own configured connection), so
unlike backlog stories it never needs the sidecar link in
customer_link_service.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from ..models.change_request import ChangeRequest, ChangeRequestCreate, ChangeRequestSourceType
from ..persistence.json_file_store import JsonFileStore
from .jira_gateway import JiraIssueSummary
from .mock_topdesk_connector import MockTopdeskConnector, TopdeskTicket


def _parse_received_at(raw: str) -> Optional[datetime]:
    if not raw:
        return None
    try:
        # Jira's own format ("...+0000") lacks the colon Python's
        # fromisoformat wants before 3.11; normalise defensively rather
        # than trust every possible Jira Cloud response shape.
        cleaned = raw.replace("Z", "+00:00")
        if len(cleaned) >= 5 and cleaned[-5] in "+-" and cleaned[-3] != ":":
            cleaned = cleaned[:-2] + ":" + cleaned[-2:]
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None


class ChangeRequestService:
    def __init__(self, directory: str) -> None:
        self._store = JsonFileStore(directory)

    def _create(
        self,
        *,
        customer_id: str,
        source_type: ChangeRequestSourceType,
        business_source: str,
        source_reference: str,
        title: str,
        raw_content: str,
        requester: str,
        request_id: Optional[str] = None,
        received_at: Optional[datetime] = None,
        source_metadata: Optional[dict[str, str]] = None,
    ) -> ChangeRequest:
        change_request = ChangeRequest(
            id=request_id or f"CR-{uuid.uuid4().hex[:8]}",
            customer_id=customer_id,
            source_type=source_type,
            business_source=business_source,  # type: ignore[arg-type]
            source_reference=source_reference,
            received_at=received_at or datetime.now(timezone.utc),
            title=title,
            raw_content=raw_content,
            attachments=[],
            requester=requester,
            status="received",
            source_metadata=source_metadata or {},
        )
        self._store.put(change_request.id, change_request.model_dump(mode="json", by_alias=False))
        return change_request

    def create_direct(
        self,
        payload: ChangeRequestCreate,
        customer_id: str,
        requester: str,
        request_id: Optional[str] = None,
    ) -> ChangeRequest:
        """request_id defaults to a random id for real intake (the
        normal POST /change-requests path); pilot-data seeding passes a
        stable id explicitly so re-seeding is idempotent rather than
        creating a duplicate every time."""
        return self._create(
            customer_id=customer_id,
            source_type=ChangeRequestSourceType.DIRECT,
            business_source=payload.business_source,
            source_reference=payload.source_reference,
            title=payload.title,
            raw_content=payload.raw_content,
            requester=requester,
            request_id=request_id,
        )

    def create_from_topdesk(
        self,
        ticket: TopdeskTicket,
        customer_id: str,
        request_id: Optional[str] = None,
        received_at: Optional[datetime] = None,
    ) -> ChangeRequest:
        """The one integration point where Topdesk-shaped data becomes
        a ChangeRequest -- MockTopdeskConnector.normalize() is the only
        function that knows Topdesk's field names; everything here
        after is the same common model and the same store as
        create_direct."""
        normalized = MockTopdeskConnector.normalize(ticket)
        return self._create(
            customer_id=customer_id,
            source_type=normalized["source_type"],
            business_source=normalized["business_source"],
            source_reference=normalized["source_reference"],
            title=normalized["title"],
            raw_content=normalized["raw_content"],
            requester=normalized["requester"],
            request_id=request_id,
            received_at=received_at,
        )

    def create_from_jira(
        self,
        issue: JiraIssueSummary,
        customer_id: str,
        request_id: str,
    ) -> ChangeRequest:
        """The one integration point where a Jira-shaped issue becomes a
        ChangeRequest -- jira_gateway.py's JiraIssueSummary is already
        the normalized common shape (analogous to
        MockTopdeskConnector.normalize()'s dict), so this just maps its
        fields onto the ChangeRequest ones. request_id is REQUIRED
        (unlike create_direct/create_from_topdesk's optional one): the
        Jira sync service always passes the stable, deterministic id it
        derives from the Jira issue key, and relies on that same id to
        detect an already-imported issue before calling this at all --
        see jira_sync_service.py."""
        return self._create(
            customer_id=customer_id,
            source_type=ChangeRequestSourceType.JIRA,
            business_source="Jira",
            source_reference=f"Jira {issue.key}",
            title=issue.summary,
            raw_content=issue.description,
            requester=issue.reporter,
            request_id=request_id,
            received_at=_parse_received_at(issue.created),
            source_metadata=dict(issue.metadata),
        )

    def list_for_customer(self, customer_id: str) -> list[ChangeRequest]:
        return [
            ChangeRequest.model_validate(doc)
            for doc in self._store.list_all()
            if doc.get("customer_id") == customer_id
        ]

    def get(self, request_id: str) -> ChangeRequest | None:
        doc = self._store.get(request_id)
        return ChangeRequest.model_validate(doc) if doc else None
