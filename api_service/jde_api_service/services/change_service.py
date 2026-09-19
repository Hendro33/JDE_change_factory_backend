"""
Assembles the frontend's Change shape from two real, independent
sources:

  1. mcp_server's backlog.py / approval.py / evidence.py file stores --
     read-only, via directory listing (the same non-invasive pattern
     review_ui.py already uses). Nothing here calls a mutating
     function in that package, and nothing in that package is modified.
  2. This service's own change_request_service, for intake items that
     have not been promoted to a story yet -- there is no
     Receive/Improve/Check orchestration in Phase 1 (deliberately out
     of scope), so a ChangeRequest simply stays a RECEIVED-state Change
     until a later phase adds that pipeline.

Every backlog-derived story is customer-scoped through
customer_link_service's sidecar. A story with no link entry is
excluded from every result here -- fail-safe, not fail-open: an
unattributed story is never guessed into a customer's view.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

from .. import config as _config  # noqa: F401  (forces the mcp_server sys.path bootstrap)
from jde_mcp_server import backlog, approval
from jde_mcp_server import config as mcp_config

from ..models.change import (
    ApprovalRecord,
    BusinessImpact,
    Change,
    EvidenceRecord,
    ExactChange,
    UserStory,
)
from ..models.change_request import ChangeRequest
from .change_request_service import ChangeRequestService
from .customer_link_service import CustomerLinkService

_VALID_SOURCES = {"Business", "Support / Topdesk", "Optimisation", "DevOps"}


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _coerce_source(raw: Optional[str]) -> str:
    return raw if raw in _VALID_SOURCES else "Business"


def _business_impact_from(raw: Optional[dict]) -> BusinessImpact:
    raw = raw or {}
    return BusinessImpact(
        financial_impact=raw.get("financial_impact") or "",
        operational_reach=raw.get("operational_reach") or "",
        risk_compliance=raw.get("risk_compliance") or "",
        strategic_alignment=raw.get("strategic_alignment") or "",
        urgency=raw.get("urgency") or "",
    )


def _evidence_records(raw_entries: list[dict]) -> list[EvidenceRecord]:
    out = []
    for i, e in enumerate(raw_entries, start=1):
        out.append(
            EvidenceRecord(
                entry_id=f"E{i}",
                stage=e.get("stage", ""),
                detail=e.get("detail", ""),
                actor=e.get("actor", ""),
                agent_versions=e.get("agent_versions"),
                captured_at=_iso(e.get("captured_at")) or "",
                prev_hash=e.get("prev_hash", ""),
                entry_hash=e.get("entry_hash", ""),
            )
        )
    return out


def _all_backlog_records() -> list[dict[str, Any]]:
    if not os.path.isdir(backlog.BACKLOG_DIR):
        return []
    out = []
    for fn in sorted(os.listdir(backlog.BACKLOG_DIR)):
        if fn.endswith(".json"):
            with open(os.path.join(backlog.BACKLOG_DIR, fn), encoding="utf-8") as f:
                out.append(json.load(f))
    return out


def _all_change_records() -> list[dict[str, Any]]:
    if not os.path.isdir(approval.CHANGE_DIR):
        return []
    out = []
    for fn in sorted(os.listdir(approval.CHANGE_DIR)):
        if fn.endswith(".json"):
            with open(os.path.join(approval.CHANGE_DIR, fn), encoding="utf-8") as f:
                out.append(json.load(f))
    return out


def _evidence_for(story_id: str) -> list[dict[str, Any]]:
    path = os.path.join(mcp_config.settings.evidence_dir, f"{story_id}.json")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _latest_change_record_for(story_id: str) -> Optional[dict[str, Any]]:
    candidates = [c for c in _all_change_records() if c.get("story_id") == story_id]
    if not candidates:
        return None
    approved = [c for c in candidates if c.get("status") == "approved"]
    pool = approved or candidates
    return max(pool, key=lambda c: c.get("created_at", 0))


def _change_from_story(record: dict[str, Any], customer_id: str) -> Change:
    story_id = record["story_id"]
    change_record = _latest_change_record_for(story_id)

    exact_change = None
    change_approval = None
    if change_record:
        op = change_record.get("operation", {})
        exact_change = ExactChange(
            tool=op.get("tool", ""),
            application=op.get("application", ""),
            version=op.get("version", ""),
            option=op.get("option", ""),
            proposed_value=str(op.get("value", "")),
            environment=change_record.get("environment", "DEV"),
            test_orchestration=op.get("test_orchestration") or "",
        )
        if change_record.get("status") in ("approved", "rejected"):
            change_approval = ApprovalRecord(
                approval_id=change_record["change_id"],
                kind="change",
                status=change_record["status"],
                change_hash=change_record.get("change_hash"),
                approved_by=change_record.get("approved_by"),
                approved_at=_iso(change_record.get("approved_at")),
                expires_at=_iso(change_record.get("expires_at")),
                note=change_record.get("decision_note"),
            )

    story_approval = None
    if record.get("decision") in ("approved", "rejected"):
        story_approval = ApprovalRecord(
            approval_id=f"AP-{story_id}-S",
            kind="story",
            status=record["decision"],
            approved_by=record.get("decided_by"),
            approved_at=_iso(record.get("decided_at")),
            note=record.get("decision_note"),
        )

    created_at = _iso(record.get("proposed_at")) or datetime.now(timezone.utc).isoformat()
    updated_at = (
        _iso(record.get("resolved_at"))
        or _iso(record.get("decided_at"))
        or created_at
    )
    statement = record.get("user_story") or story_id

    return Change(
        id=story_id,
        customer_id=customer_id,
        title=statement[:120],
        source=_coerce_source(record.get("source")),
        source_reference="",
        original_request=statement,
        state=record.get("state", "BACKLOG_READY"),
        business_impact=_business_impact_from(record.get("business_impact")),
        complexity_signal=record.get("rough_complexity_signal") or "Unknown",
        created_at=created_at,
        updated_at=updated_at,
        updated_by=record.get("decided_by") or record.get("resolved_by") or "",
        user_story=UserStory(statement=statement, quality_status="passed"),
        story_approval=story_approval,
        exact_change=exact_change,
        change_approval=change_approval,
        evidence=_evidence_records(_evidence_for(story_id)),
    )


def _change_from_request(cr: ChangeRequest) -> Change:
    received_iso = cr.received_at.isoformat()
    return Change(
        id=cr.id,
        customer_id=cr.customer_id,
        title=cr.title,
        source=cr.business_source,
        source_reference=cr.source_reference,
        original_request=cr.raw_content,
        state="RECEIVED",
        created_at=received_iso,
        updated_at=received_iso,
        updated_by=cr.requester,
        evidence=[],
    )


class ChangeService:
    def __init__(
        self,
        change_request_service: ChangeRequestService,
        customer_link_service: CustomerLinkService,
    ) -> None:
        self._change_requests = change_request_service
        self._links = customer_link_service

    def _stories_for_customer(self, customer_id: str) -> list[Change]:
        out = []
        for record in _all_backlog_records():
            story_id = record["story_id"]
            if self._links.customer_for(story_id) != customer_id:
                continue
            out.append(_change_from_story(record, customer_id))
        return out

    def list_for_customer(self, customer_id: str) -> list[Change]:
        stories = self._stories_for_customer(customer_id)
        requests = [
            _change_from_request(cr) for cr in self._change_requests.list_for_customer(customer_id)
        ]
        return sorted(stories + requests, key=lambda c: c.updated_at, reverse=True)

    def get_for_customer(self, change_id: str, customer_id: str) -> Optional[Change]:
        if change_id.startswith("CR-"):
            cr = self._change_requests.get(change_id)
            if cr is None or cr.customer_id != customer_id:
                return None
            return _change_from_request(cr)

        for record in _all_backlog_records():
            if record["story_id"] == change_id:
                if self._links.customer_for(change_id) != customer_id:
                    return None
                return _change_from_story(record, customer_id)
        return None

    def backlog_for_customer(self, customer_id: str) -> list[Change]:
        return [c for c in self._stories_for_customer(customer_id) if c.state == "BACKLOG_READY"]
