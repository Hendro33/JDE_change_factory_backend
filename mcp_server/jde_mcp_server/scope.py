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


def scope_revision() -> str:
    """This engagement's own scope.json revision -- stamped onto every
    exact-change record at propose time (approval.py), same purpose as
    capability_catalog.catalog_revision(). Informational only (never a
    gate) -- unlike every check_* function in this module, a missing
    scope.json here just means "unknown," not a ScopeViolation. A story
    can be proposed before scope.json exists for a brand-new engagement;
    it simply cannot be WRITTEN (require_exact_change's own
    check_environment_binding call) until one does."""
    try:
        return _load_scope().get("scope_revision", "unknown")
    except ScopeViolation:
        return "unknown"


def check_functional_scope(application: str, version: str, option: str) -> dict:
    """Raises ScopeViolation unless (application, version, option) is
    explicitly listed in this engagement's approved scope (Appendix
    D.2). Returns the matching scope entry (which now also carries the
    capability_id/capability_revision this entry is bound to -- design
    update Section 5.1's "Capability binding" -- plus any allowed_values
    list) on success."""
    scope = _load_scope()
    for entry in scope.get("functional_agent", {}).get("approved_versions", []):
        if (
            entry.get("application", "").upper() == application.upper()
            and entry.get("version", "").upper() == version.upper()
            and option.upper() in [o.upper() for o in entry.get("options", [])]
        ):
            if not entry.get("capability_id"):
                raise ScopeViolation(
                    f"the scope.json entry for {application}/{version}/{option} has no "
                    "capability_id -- every approved_versions entry must be bound to a "
                    "capability from capability_catalog.json (design update Section 5.1). "
                    "Add capability_id/capability_revision to this entry before it can be used."
                )
            return entry
    raise ScopeViolation(
        f"{application}/{version}/{option} is not in this engagement's "
        f"approved scope ({SCOPE_FILE}). Add it to scope.json, sourced from "
        "a signed-off Configuration Guidelines document (Appendix D), "
        "before this write can be attempted -- a story being approved in "
        "Phase 2 (Section 3.5) is not the same as this specific operation "
        "being in scope."
    )


# ---------------------------------------------------------------------
# Environment binding (design update Section 5.1). DEV isolation is a
# fact to be demonstrated per engagement, not inferred from an
# environment simply being named DEV (Section 1's own warning about
# OCM mappings and shared business data).
# ---------------------------------------------------------------------

def check_environment_binding(environment: str) -> dict:
    """Raises ScopeViolation unless 'environment' is DEV and this
    engagement's scope.json records a confirmed, non-empty DEV
    environment binding. This is deliberately stricter than just
    checking the string "DEV" -- an engagement that hasn't actually
    demonstrated isolation (environment.isolation_confirmed) has not
    cleared Section 1's mandatory boundary, regardless of what
    environment name a caller passes."""
    if environment != "DEV":
        raise ScopeViolation(
            f"'{environment}' is not DEV. All JDE access and execution use "
            "approved DEV endpoints only -- this is a universal rule, not "
            "engagement-configurable."
        )
    scope = _load_scope()
    env = scope.get("environment", {})
    if not env.get("dev_environment_id") or not env.get("dev_path_code"):
        raise ScopeViolation(
            f"scope.json ({SCOPE_FILE}) has no dev_environment_id/dev_path_code "
            "configured -- DEV isolation must be demonstrated, not assumed. Fill "
            "in the environment section (Appendix D.2 extension, Section 5.1) "
            "before any write can be attempted."
        )
    if not env.get("isolation_confirmed"):
        raise ScopeViolation(
            f"scope.json ({SCOPE_FILE}) has environment.isolation_confirmed=false. "
            "An environment named DEV is not sufficient on its own -- its OCM "
            "mappings and business-data sources must be confirmed not to affect "
            "another environment (Section 1) before this flips to true. This is "
            "a human decision, recorded once isolation has actually been checked, "
            "never something an agent sets for itself."
        )
    return env


def find_spike_experiment(capability_id: str, application: str, version: str, option: str, environment: str) -> Optional[dict]:
    """Returns the matching spike_experiments entry, if this engagement
    has explicitly approved a bounded DEV validation experiment for
    this exact capability + target (design update Section 2/3) -- or
    None. A Needs-spike capability can still be exercised, but only via
    an entry here; approval.py passes the result of this check into
    capability_catalog.require_executable, never trusting the agent's
    own claim that a spike was approved."""
    scope = _load_scope()
    for entry in scope.get("functional_agent", {}).get("spike_experiments", []):
        if (
            entry.get("capability_id") == capability_id
            and entry.get("application", "").upper() == application.upper()
            and entry.get("version", "").upper() == version.upper()
            and (not option or entry.get("option", "").upper() == option.upper())
            and entry.get("environment", "DEV") == environment
        ):
            return entry
    return None


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
