"""
Persistence for DecisionFeedback. Append-only: record() always writes a
new document under a fresh id, never updates a prior one -- see
models/decision_feedback.py for why this exists.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from ..models.decision_feedback import DecisionFeedback, DecisionFeedbackKind, FeedbackReasonCode
from ..persistence.json_file_store import JsonFileStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DecisionFeedbackService:
    def __init__(self, directory: str) -> None:
        self._store = JsonFileStore(directory)

    def record(
        self,
        *,
        change_id: str,
        customer_id: str,
        kind: DecisionFeedbackKind,
        decided_by: str,
        identity_id: Optional[str] = None,
        reason_code: Optional[FeedbackReasonCode] = None,
        note: str = "",
    ) -> DecisionFeedback:
        entry = DecisionFeedback(
            id=f"{change_id}-{kind}-{uuid.uuid4().hex[:8]}",
            change_id=change_id,
            customer_id=customer_id,
            kind=kind,
            decided_by=decided_by,
            identity_id=identity_id,
            reason_code=reason_code,
            note=note,
            recorded_at=_now(),
        )
        self._store.put(entry.id, entry.model_dump(mode="json", by_alias=False))
        return entry

    def list_for_customer(self, customer_id: str) -> list[DecisionFeedback]:
        return [
            DecisionFeedback.model_validate(doc)
            for doc in self._store.list_all()
            if doc.get("customer_id") == customer_id
        ]

    def list_all(self) -> list[DecisionFeedback]:
        return [DecisionFeedback.model_validate(doc) for doc in self._store.list_all()]
