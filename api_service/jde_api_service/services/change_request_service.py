"""
ChangeRequest persistence.

One store, multiple ways to write into it -- create_direct (a person
using the "New change request" form) and create_from_topdesk (the mock
Topdesk connector's normalized output) both converge on the same
_create() and the same JsonFileStore. There is no separate ticket
database and no separate workflow for Topdesk; a Topdesk-sourced
record is a ChangeRequest exactly like a direct one, distinguished only
by its sourceType field.

A ChangeRequest is customer-scoped from the moment it's created (the
customer_id comes from the already-validated X-Customer-Id for real
intake, or is supplied directly by the pilot-data seeder), so unlike
backlog stories it never needs the sidecar link in customer_link_service.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from ..models.change_request import ChangeRequest, ChangeRequestCreate, ChangeRequestSourceType
from ..persistence.json_file_store import JsonFileStore
from .mock_topdesk_connector import MockTopdeskConnector, TopdeskTicket


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

    def list_for_customer(self, customer_id: str) -> list[ChangeRequest]:
        return [
            ChangeRequest.model_validate(doc)
            for doc in self._store.list_all()
            if doc.get("customer_id") == customer_id
        ]

    def get(self, request_id: str) -> ChangeRequest | None:
        doc = self._store.get(request_id)
        return ChangeRequest.model_validate(doc) if doc else None
