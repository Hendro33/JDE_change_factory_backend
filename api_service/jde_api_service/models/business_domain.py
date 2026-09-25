"""
BusinessDomain -- the customer's own business taxonomy, kept distinct
from three things it must never be conflated with:

  1. The APQC Process Classification Framework itself. `apqc_code` and
     `level` place a domain within APQC, but this model is not an APQC
     catalogue entry -- it is the customer's domain, which happens to
     be classified against APQC. The full catalogue is deliberately
     not loaded (out of scope for this increment); domains are created
     one at a time, by or for a customer, as needed.
  2. Customer-specific domain requirements/knowledge -- that lives on
     the Change/UserStory itself (business_context, open_questions),
     never folded into the domain record.
  3. Authorisation/governance roles -- `domain_owner` here is a NAME
     for display and record-keeping, exactly like `decided_by` on an
     ApprovalRecord. It is not an access-control list, and nothing in
     this API checks a caller's identity against it (Section 15.10:
     full RBAC is a target-architecture NFR, not built here). That is
     the hook a later phase can build real authorisation on, not an
     enforcement mechanism itself.
"""

from __future__ import annotations

from typing import Literal, Optional

from .base import ApiModel

BusinessDomainStatus = Literal["active", "proposed", "retired"]


class BusinessDomain(ApiModel):
    id: str
    customer_id: str
    apqc_code: str
    name: str
    # APQC Level 2 ("4.4") or Level 3 ("4.4.3") -- dotted depth, not a
    # free-text label. Level 1 (single category, e.g. "4.0") and the
    # full catalogue below Level 3 are out of scope for this increment.
    level: str
    description: str = ""
    # Legacy free-text note, kept for existing records and never used for
    # authority. Who may act as Domain Owner comes only from
    # domain_assignments (Admin > Users); see assigned_owners.
    domain_owner: str = ""
    status: BusinessDomainStatus = "active"
    # Derived on read from domain_assignments: active members holding the
    # domain_owner role who are assigned to this domain. Never stored.
    assigned_owners: list[str] = []
    # Records created before revisions existed load as revision 1.
    revision: int = 1
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None


class BusinessDomainCreate(ApiModel):
    apqc_code: str
    name: str
    level: str
    description: str = ""
    domain_owner: str = ""


class BusinessDomainStatusUpdate(ApiModel):
    status: BusinessDomainStatus
    # The revision the client loaded; a different current revision is a 409.
    expected_revision: Optional[int] = None
