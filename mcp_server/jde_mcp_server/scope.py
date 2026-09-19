"""
The boundaries a guideline document alone can't enforce.

Two layers here, and they are not the same thing:

1. UNIVERSAL rules -- hardcoded below, true for every JDE engagement
   regardless of customer, sourced from Oracle's own documentation
   (Appendix B.4 / D.1 / E.1 of the design document). These are never
   customer-configurable, on purpose: they aren't this engagement's
   preference, they're how JDE itself works.

2. ENGAGEMENT scope -- loaded from scope.json, filled in once per
   customer from the Configuration Guidelines (Appendix D) and
   Development Guidelines (Appendix E). This is the "permitted
   operations allowlist" referred to in Section 4.4: a human decides
   what's in scope, in a document; this module is what makes that
   decision an enforced fact rather than a paragraph the agent is
   trusting itself to remember.

Both layers apply to every write. Neither is optional, and neither
substitutes for the other -- a version could pass the engagement scope
check and still be rejected by the universal check, or vice versa.
"""

from __future__ import annotations

import json
import os
import re
from typing import Optional

SCOPE_FILE = os.environ.get("JDE_SCOPE_FILE", "./scope.json")

# ---------------------------------------------------------------------
# UNIVERSAL rules (Appendix B.4) -- never customer-configurable.
# ---------------------------------------------------------------------

# XJDE = Oracle-owned batch/UBE template versions. ZJDE = Oracle-owned
# interactive default versions. Both are overwritten on upgrade and
# are templates to copy, never targets to write to directly (Oracle
# JD Edwards EnterpriseOne Tools Report Design Aid Guide; Change
# Management "Versions" page -- Appendix C).
_ORACLE_OWNED_VERSION = re.compile(r"^(XJDE|ZJDE)", re.IGNORECASE)

# Product/system codes 55-59 are the range Oracle reserves for
# customer-created custom objects (Table Design Guide, Report Design
# Aid Guide, "Copying Objects" -- Appendix C). A Technical Agent
# proposing an object outside this range is proposing to create
# something in Oracle's own namespace, which is never acceptable
# regardless of what any engagement's scope file says.
_RESERVED_CUSTOM_CODE_RANGE = range(55, 60)


class ScopeViolation(RuntimeError):
    """Raised when an operation fails a universal or engagement-scope
    check. Distinct from StoryNotApproved (backlog.py) -- this is about
    *what* is being requested, not *who* approved it. Both can apply to
    the same call."""


def reject_if_oracle_owned_version(version: str) -> None:
    if _ORACLE_OWNED_VERSION.match(version.strip()):
        raise ScopeViolation(
            f"'{version}' is an Oracle-owned template version (XJDE/ZJDE). "
            "These are overwritten on upgrade and must never be written to "
            "directly -- copy to a customer-named version first (a manual "
            "OMW step; see Appendix D.1). This check is universal and does "
            "not depend on the engagement's scope.json."
        )


def check_custom_product_code(product_code: str) -> None:
    try:
        code = int(product_code)
    except ValueError:
        raise ScopeViolation(f"product code '{product_code}' is not numeric -- cannot verify it is in the reserved custom range 55-59.")
    if code not in _RESERVED_CUSTOM_CODE_RANGE:
        raise ScopeViolation(
            f"product code {code} is outside the 55-59 range Oracle reserves "
            "for customer objects (Appendix E.1). Creating or modifying an "
            "object in Oracle's own numbering range is never acceptable, "
            "regardless of engagement scope."
        )


# ---------------------------------------------------------------------
# ENGAGEMENT scope -- loaded from scope.json (Appendix D.2 / E.2).
# ---------------------------------------------------------------------

def _load_scope() -> dict:
    if not os.path.exists(SCOPE_FILE):
        raise ScopeViolation(
            f"No scope.json found at {SCOPE_FILE}. The Configuration and "
            "Development Guidelines (Appendix D/E) must be filled in and "
            "extracted into a scope file before any write tool can run -- "
            "see scope.example.json for the shape. This is not optional: "
            "there is no 'no restrictions configured' default."
        )
    with open(SCOPE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def check_functional_scope(application: str, version: str, option: str) -> dict:
    """Raises ScopeViolation unless (application, version, option) is
    explicitly listed in this engagement's approved scope (Appendix
    D.2). Returns the matching scope entry (which may carry an
    allowed_values list) on success."""
    scope = _load_scope()
    for entry in scope.get("functional_agent", {}).get("approved_versions", []):
        if (
            entry.get("application", "").upper() == application.upper()
            and entry.get("version", "").upper() == version.upper()
            and option.upper() in [o.upper() for o in entry.get("options", [])]
        ):
            return entry
    raise ScopeViolation(
        f"{application}/{version}/{option} is not in this engagement's "
        f"approved scope ({SCOPE_FILE}). Add it to scope.json, sourced from "
        "a signed-off Configuration Guidelines document (Appendix D), "
        "before this write can be attempted -- a story being approved in "
        "Phase 2 (Section 3.5) is not the same as this specific operation "
        "being in scope."
    )


def check_allowed_value(entry: dict, value: str) -> None:
    allowed = entry.get("allowed_values")
    if allowed and value not in allowed:
        raise ScopeViolation(
            f"'{value}' is not one of this option's allowed values in "
            f"scope.json ({allowed}). The scope entry constrains not just "
            "which option can be touched, but what it can be set to."
        )


def check_technical_scope(object_type: str) -> dict:
    """Raises ScopeViolation unless object_type is one this engagement
    has actually authorised (Appendix E.2) -- separate from whether
    Oracle has validated the mechanism at all (Section 7.6). Validated
    and authorised are different questions; this checks the second."""
    scope = _load_scope()
    authorised = scope.get("technical_agent", {}).get("authorized_object_types", [])
    if object_type not in authorised:
        raise ScopeViolation(
            f"'{object_type}' is not an authorised object type for this "
            f"engagement ({authorised or 'none configured'}). Technical "
            "feasibility (Section 7.6) and customer authorisation "
            "(Appendix E.2) are two separate gates -- both must pass."
        )
    return scope["technical_agent"]
