"""
The boundaries a guideline document alone can't enforce.

Two layers here, and they are not the same thing:

1. UNIVERSAL rules -- hardcoded below, true for every JDE engagement
   regardless of customer, sourced from Oracle's own documentation
   (Appendix B.4 / D.1 / E.1 of the design document). These are never
   customer-configurable, on purpose: they aren't this engagement's
   preference, they're how JDE itself works.

2. ENGAGEMENT scope -- the company's own record, saved by an Admin
   under Admin > ERP / JDE Landscape (api_service's EngagementScope,
   one JSON file per company in JDE_COMPANY_SCOPE_DIR) and filled in
   from the Configuration Guidelines (Appendix D) and Development
   Guidelines (Appendix E). This is the "permitted
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
from datetime import datetime, timezone
from typing import Optional

# Both directories are written by api_service, never by an agent:
#   COMPANY_SCOPE_DIR  -- <company_id>.json, the Admin-saved EngagementScope
#   STORY_COMPANY_DIR  -- <story_id>.json, {"customer_id": ...}, recorded at intake
# The company a write belongs to is always derived from the story's
# recorded link, never from anything a caller passes in. Unset or
# missing means nothing can execute. (api_service's startup points
# these at its own data directory; see main.py.)
COMPANY_SCOPE_DIR = os.environ.get("JDE_COMPANY_SCOPE_DIR", "")
STORY_COMPANY_DIR = os.environ.get("JDE_STORY_COMPANY_DIR", "")

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
            "not depend on the engagement's scope."
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
# ENGAGEMENT scope -- per company (Appendix D.2 / E.2).
# ---------------------------------------------------------------------

def _read_json(directory: str, doc_id: str) -> Optional[dict]:
    path = os.path.join(directory, f"{doc_id.replace('/', '_')}.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def company_for_story(story_id: str) -> str:
    """The company this story was recorded against at intake. Raises
    ScopeViolation if that cannot be established -- an unattributed
    story is never guessed into a company."""
    if not STORY_COMPANY_DIR:
        raise ScopeViolation(
            "JDE_STORY_COMPANY_DIR is not configured, so the company a story "
            "belongs to cannot be established. Nothing can execute until it is."
        )
    link = _read_json(STORY_COMPANY_DIR, story_id)
    company_id = (link or {}).get("customer_id")
    if not company_id:
        raise ScopeViolation(
            f"story {story_id} is not linked to a company. Only stories taken in "
            "through Jade (which records the company) can be executed."
        )
    return company_id


def load_company_scope(company_id: str) -> dict:
    """The company's saved engagement scope. There is no 'no restrictions
    configured' default: unset directory or no saved record blocks."""
    if not COMPANY_SCOPE_DIR:
        raise ScopeViolation(
            "JDE_COMPANY_SCOPE_DIR is not configured, so no company's engagement "
            "scope can be read. Nothing can execute until it is."
        )
    scope = _read_json(COMPANY_SCOPE_DIR, company_id)
    if scope is None:
        raise ScopeViolation(
            f"company {company_id} has no saved engagement scope. An Admin must "
            "save one under Admin > ERP / JDE Landscape before any write can run."
        )
    if scope.get("customer_id") not in (None, company_id):
        raise ScopeViolation(f"the scope record for {company_id} names a different company -- refusing.")
    return scope


def scope_revision(company_id: Optional[str]) -> str:
    """Informational only (stamped on change records), never a gate:
    'unknown' when the scope can't be read."""
    if not company_id:
        return "unknown"
    try:
        return str(load_company_scope(company_id).get("revision", "unknown"))
    except ScopeViolation:
        return "unknown"


def check_functional_scope(scope: dict, application: str, version: str, option: str) -> dict:
    """Raises ScopeViolation unless (application, version, option) is
    explicitly listed in this company's approved scope (Appendix D.2).
    Returns the matching entry (with its capability binding and any
    allowed_values list)."""
    for entry in scope.get("functional_agent", {}).get("approved_versions", []):
        if (
            entry.get("application", "").upper() == application.upper()
            and entry.get("version", "").upper() == version.upper()
            and option.upper() in [o.upper() for o in entry.get("options", [])]
        ):
            if not entry.get("capability_id"):
                raise ScopeViolation(
                    f"the approved-versions entry for {application}/{version}/{option} has no "
                    "capability_id -- every entry must be bound to a catalogue capability "
                    "(design update Section 5.1) before it can be used."
                )
            return entry
    raise ScopeViolation(
        f"{application}/{version}/{option} is not in company "
        f"{scope.get('customer_id', '?')}'s approved scope. A story being approved "
        "(Section 3.5) is not the same as this specific operation being in scope."
    )


# ---------------------------------------------------------------------
# Environment binding (design update Section 5.1). DEV isolation is a
# fact to be demonstrated per company, not inferred from an
# environment simply being named DEV (Section 1's own warning about
# OCM mappings and shared business data).
# ---------------------------------------------------------------------

def check_environment_binding(scope: dict, environment: str) -> dict:
    """Raises ScopeViolation unless 'environment' is DEV and this
    company's scope records a confirmed, non-empty DEV binding."""
    if environment != "DEV":
        raise ScopeViolation(
            f"'{environment}' is not DEV. All JDE access and execution use "
            "approved DEV endpoints only -- this is a universal rule, not "
            "engagement-configurable."
        )
    env = scope.get("environment") or {}
    if not env.get("dev_environment_id") or not env.get("dev_path_code"):
        raise ScopeViolation(
            "this company's scope has no DEV environment id / path code -- DEV "
            "isolation must be demonstrated, not assumed."
        )
    if not env.get("isolation_confirmed"):
        raise ScopeViolation(
            "this company's scope does not confirm DEV isolation. An environment "
            "named DEV is not sufficient on its own -- its OCM mappings and "
            "business-data sources must be confirmed not to affect another "
            "environment (Section 1). This is a human decision, never an agent's."
        )
    return env


def _parse_instant(value) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None  # ambiguous -- treated as missing
    return parsed


def find_spike_experiment(
    scope: dict,
    capability_id: str,
    capability_revision: str,
    application: str,
    version: str,
    option: str,
    environment: str,
    now: Optional[datetime] = None,
) -> Optional[dict]:
    """The matching, CURRENT spike_experiments entry (a dated, explicitly
    approved DEV test window for this exact capability revision and
    target), or None. An entry with no expiry, an unreadable expiry or
    one in the past allows nothing."""
    now = now or datetime.now(timezone.utc)
    for entry in scope.get("functional_agent", {}).get("spike_experiments", []):
        if (
            entry.get("capability_id") == capability_id
            and entry.get("capability_revision") == capability_revision
            and entry.get("application", "").upper() == application.upper()
            and entry.get("version", "").upper() == version.upper()
            and (not option or entry.get("option", "").upper() == option.upper())
            and entry.get("environment", "DEV") == environment
        ):
            expires = _parse_instant(entry.get("expires_at"))
            if expires is not None and expires > now:
                return entry
    return None


def check_allowed_value(entry: dict, value: str) -> None:
    allowed = entry.get("allowed_values")
    if allowed and value not in allowed:
        raise ScopeViolation(
            f"'{value}' is not one of this option's allowed values ({allowed}). "
            "The scope entry constrains not just which option can be touched, "
            "but what it can be set to."
        )


def check_technical_scope(scope: dict, object_type: str) -> dict:
    """Raises ScopeViolation unless object_type is one this company has
    actually authorised (Appendix E.2) -- separate from whether Oracle
    has validated the mechanism at all (Section 7.6)."""
    technical = scope.get("technical_agent") or {}
    authorised = technical.get("authorized_object_types", [])
    if object_type not in authorised:
        raise ScopeViolation(
            f"'{object_type}' is not an authorised object type for this "
            f"company ({authorised or 'none configured'}). Technical "
            "feasibility (Section 7.6) and customer authorisation "
            "(Appendix E.2) are two separate gates -- both must pass."
        )
    return technical


# ---------------------------------------------------------------------
# Approval policy -- who may approve an exact change for this company,
# and for how long that approval stays valid. Saved by an Admin with
# the rest of the scope. Missing or not understood blocks: a policy
# field this code does not know could be a restriction it would
# otherwise silently ignore.
# ---------------------------------------------------------------------

KNOWN_POLICY_VERSIONS = {1}
APPROVER_ROLES = {"admin", "product_manager", "domain_owner"}
_POLICY_FIELDS = {"policy_version", "exact_change_approver_roles", "approval_valid_hours"}
MAX_APPROVAL_VALID_HOURS = 168


def require_approval_policy(scope: dict) -> dict:
    policy = scope.get("approval_policy")
    company = scope.get("customer_id", "?")
    if not policy:
        raise ScopeViolation(
            f"company {company} has no approval policy, so nobody is authorised to "
            "approve an exact change and nothing can execute. An Admin must set one "
            "under Admin > ERP / JDE Landscape."
        )
    unknown = set(policy) - _POLICY_FIELDS
    if unknown:
        raise ScopeViolation(f"company {company}'s approval policy has fields this gate does not understand ({sorted(unknown)}) -- refusing.")
    if policy.get("policy_version") not in KNOWN_POLICY_VERSIONS:
        raise ScopeViolation(f"company {company}'s approval policy version {policy.get('policy_version')!r} is not one this gate understands -- refusing.")
    roles = policy.get("exact_change_approver_roles")
    if not isinstance(roles, list) or not roles or any(r not in APPROVER_ROLES for r in roles):
        raise ScopeViolation(
            f"company {company}'s approval policy must name at least one approver role "
            f"from {sorted(APPROVER_ROLES)}; got {roles!r} -- refusing."
        )
    hours = policy.get("approval_valid_hours")
    if not isinstance(hours, int) or isinstance(hours, bool) or not 1 <= hours <= MAX_APPROVAL_VALID_HOURS:
        raise ScopeViolation(
            f"company {company}'s approval policy must give approval_valid_hours between 1 and "
            f"{MAX_APPROVAL_VALID_HOURS}; got {hours!r} -- refusing."
        )
    return policy
