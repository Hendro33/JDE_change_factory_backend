"""
ChangeRequest -- the common intake model (approved analysis, Section 6).

Every source connector's only job is producing one of these; the
downstream Change Factory workflow (Receive/Improve/Check onward)
operates on this common shape regardless of where it came from.
Phase 1 implements the DIRECT source and the Jira connector
(services/jira_sync_service.py) -- Topdesk and file-upload connectors
remain future work that will populate the same model.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Optional

from .base import ApiModel
from .change import ChangeSource


class ChangeRequestSourceType(str, Enum):
    DIRECT = "DIRECT"
    TOPDESK = "TOPDESK"
    JIRA = "JIRA"
    FILE_UPLOAD = "FILE_UPLOAD"


class Attachment(ApiModel):
    filename: str
    content_type: str
    storage_ref: str


class ChangeRequest(ApiModel):
    id: str
    customer_id: str
    source_type: ChangeRequestSourceType
    # Business-facing categorisation (design doc Section 3.1) -- distinct
    # from source_type, which is the technical intake channel. This is
    # what the existing "New change request" form already collects and
    # what Change.source (src/types/domain.ts) expects.
    business_source: ChangeSource
    source_reference: str = ""
    received_at: datetime
    title: str
    raw_content: str
    attachments: list[Attachment] = []
    requester: str
    status: Literal["received", "handed_off", "duplicate", "rejected_at_intake"] = "received"
    # Free-form context carried verbatim from the source connector (e.g.
    # Jira's Request Type / Work Type / Priority) -- imported for display
    # only. Nothing in this service reads these values to decide routing
    # or classification; that stays ITSM's/Jira's decision, made before
    # a ticket ever reaches the configured pickup status.
    source_metadata: dict[str, str] = {}


class ChangeRequestCreate(ApiModel):
    """What the frontend actually sends. source_type is NOT accepted
    here -- this endpoint is DIRECT-only in Phase 1, so the server sets
    it, not the client (Section: 'Do not implement Topdesk, file
    upload... yet' -- accepting a sourceType here would silently promise
    connectors that don't exist)."""

    title: str
    business_source: ChangeSource
    source_reference: str = ""
    raw_content: str
    # Pending uploads (POST /change-requests/attachments) to link to the new
    # request; each must belong to this customer and this user.
    attachment_ids: list[str] = []
