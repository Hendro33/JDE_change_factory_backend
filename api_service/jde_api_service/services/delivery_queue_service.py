"""
DeliveryQueueEntry persistence -- same JsonFileStore-per-directory
sidecar pattern as domain_review_service.py / customer_link_service.py.
One entry per change_id (a change is either queued or it isn't -- this
increment does not model re-queuing history).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ..models.delivery_queue import DeliveryQueueEntry, DeliveryStatus
from ..persistence.json_file_store import JsonFileStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DeliveryQueueService:
    def __init__(self, directory: str) -> None:
        self._store = JsonFileStore(directory)

    def get(self, change_id: str) -> Optional[DeliveryQueueEntry]:
        doc = self._store.get(change_id)
        return DeliveryQueueEntry.model_validate(doc) if doc else None

    def list_for_customer(self, customer_id: str) -> list[DeliveryQueueEntry]:
        entries = [
            DeliveryQueueEntry.model_validate(doc)
            for doc in self._store.list_all()
            if doc.get("customer_id") == customer_id
        ]
        return sorted(entries, key=lambda e: e.position)

    def add(self, change_id: str, customer_id: str, added_by: str, note: str = "") -> DeliveryQueueEntry:
        """Idempotent: adding an already-queued change returns the
        existing entry unchanged rather than duplicating or reordering
        it -- exactly like customer_link_service.link()."""
        existing = self.get(change_id)
        if existing is not None:
            return existing
        position = len(self.list_for_customer(customer_id)) + 1
        entry = DeliveryQueueEntry(
            change_id=change_id,
            customer_id=customer_id,
            position=position,
            status="queued",
            added_by=added_by,
            added_at=_now(),
            note=note,
        )
        self._store.put(change_id, entry.model_dump(mode="json", by_alias=False))
        return entry

    def set_status(self, change_id: str, status: DeliveryStatus, blocked_reason: str = "") -> Optional[DeliveryQueueEntry]:
        entry = self.get(change_id)
        if entry is None:
            return None
        entry.status = status
        entry.blocked_reason = blocked_reason if status == "blocked" else None
        self._store.put(change_id, entry.model_dump(mode="json", by_alias=False))
        return entry
