"""
BusinessDomain persistence. Same JsonFileStore-per-directory pattern as
every other api_service-owned store (change_request_service.py,
customer_link_service.py) -- see json_file_store.py's own docstring for
why: this pilot's maturity calls for plain files, not a database
(Section 18).

Every read is customer-scoped, matching the rule CustomerScope's own
docstring on the frontend states: a domain belonging to another
customer must not be visible or selectable, full stop.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from ..models.business_domain import BusinessDomain, BusinessDomainCreate, BusinessDomainStatus
from ..persistence.json_file_store import JsonFileStore
from ..persistence.revisions import next_revision


class BusinessDomainService:
    def __init__(self, directory: str) -> None:
        self._store = JsonFileStore(directory)

    def create(
        self, payload: BusinessDomainCreate, customer_id: str, domain_id: str | None = None, actor: str | None = None
    ) -> BusinessDomain:
        """domain_id defaults to a random id; pilot seeding passes a
        stable one so re-seeding is idempotent (same convention as
        change_request_service.create_direct)."""
        domain = BusinessDomain(
            id=domain_id or f"DOM-{uuid.uuid4().hex[:8]}",
            customer_id=customer_id,
            apqc_code=payload.apqc_code,
            name=payload.name,
            level=payload.level,
            description=payload.description,
            domain_owner=payload.domain_owner,
            revision=1,
            updated_at=datetime.now(timezone.utc).isoformat(),
            updated_by=actor,
        )
        self._store.put(domain.id, domain.model_dump(mode="json", by_alias=False))
        return domain

    def list_for_customer(self, customer_id: str) -> list[BusinessDomain]:
        return [
            BusinessDomain.model_validate(doc)
            for doc in self._store.list_all()
            if doc.get("customer_id") == customer_id
        ]

    def get_for_customer(self, domain_id: str, customer_id: str) -> BusinessDomain | None:
        doc = self._store.get(domain_id)
        if doc is None or doc.get("customer_id") != customer_id:
            # Same "does not exist for this caller" rule as
            # change_service.get_for_customer -- a domain belonging to
            # another customer must look identical to a missing one.
            return None
        return BusinessDomain.model_validate(doc)

    def update_status(
        self, domain_id: str, status: BusinessDomainStatus, expected_revision: int | None, actor: str
    ) -> BusinessDomain:
        """Caller (the router) must have already confirmed the domain
        belongs to the caller's customer via get_for_customer -- this
        method itself trusts domain_id, same convention append_version
        etc. use elsewhere once existence/ownership is already checked.
        Raises RevisionConflict/RevisionRequired on a stale save."""
        with self._store.locked():
            doc = self._store.get(domain_id)
            assert doc is not None
            domain = BusinessDomain.model_validate(doc)
            domain.revision = next_revision(domain.revision, expected_revision)
            domain.status = status
            domain.updated_at = datetime.now(timezone.utc).isoformat()
            domain.updated_by = actor
            self._store.put(domain.id, domain.model_dump(mode="json", by_alias=False))
            return domain
