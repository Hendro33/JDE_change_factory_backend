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

from typing import Literal

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
    domain_owner: str = ""
    status: BusinessDomainStatus = "active"


class BusinessDomainCreate(ApiModel):
    apqc_code: str
    name: str
    level: str
    description: str = ""
    domain_owner: str = ""
