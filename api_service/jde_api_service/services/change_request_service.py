"""
ChangeRequest persistence -- Phase 1 supports DIRECT entry only.

A ChangeRequest is customer-scoped from the moment it's created (the
customer_id comes from the already-validated X-Customer-Id, never from
the request body), so unlike backlog stories it never needs the
sidecar link in customer_link_service.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from ..models.change_request import ChangeRequest, ChangeRequestCreate, ChangeRequestSourceType
from ..persistence.json_file_store import JsonFileStore


class ChangeRequestService:
    def __init__(self, directory: str) -> None:
        self._store = JsonFileStore(directory)

    def create_direct(self, payload: ChangeRequestCreate, customer_id: str, requester: str) -> ChangeRequest:
        request_id = f"CR-{uuid.uuid4().hex[:8]}"
        change_request = ChangeRequest(
            id=request_id,
            customer_id=customer_id,
            source_type=ChangeRequestSourceType.DIRECT,
            business_source=payload.business_source,
            source_reference=payload.source_reference,
            received_at=datetime.now(timezone.utc),
            title=payload.title,
            raw_content=payload.raw_content,
            attachments=[],
            requester=requester,
            status="received",
        )
        self._store.put(request_id, change_request.model_dump(mode="json", by_alias=False))
        return change_request

    def list_for_customer(self, customer_id: str) -> list[ChangeRequest]:
        return [
            ChangeRequest.model_validate(doc)
            for doc in self._store.list_all()
            if doc.get("customer_id") == customer_id
        ]

    def get(self, request_id: str) -> ChangeRequest | None:
        doc = self._store.get(request_id)
        return ChangeRequest.model_validate(doc) if doc else None
