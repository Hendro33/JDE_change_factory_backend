"""
Exact-change Approval Record (design document Sections 15.3, 16.2, 17.1).

Gate 2 (backlog.py) establishes that a human approved the STORY. This
module establishes something stricter, and separate: that a human
approved this EXACT operation -- this application, this version, this
option, this value, in this environment -- not just "some change to
this approved story, whatever it turns out to be."

Why this needs to be its own control, not folded into backlog.py: a
story remaining "approved" is not permission to execute a DIFFERENT
implementation than the one a human actually saw. Without this, a
Functional Agent could get a story approved for one purpose and then
execute a different value or option against that same approved
story_id, and every check in backlog.py/scope.py would still pass.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from typing import Iterable, Optional

from .backlog import require_approved, BacklogError
from . import authority, capability_catalog
from .scope import (
    check_environment_binding,
    company_for_story,
    find_spike_experiment,
    load_company_scope,
    require_approval_policy,
    scope_revision,
)

CHANGE_DIR = os.environ.get("JDE_CHANGE_DIR", "./changes")

class ChangeApprovalError(RuntimeError):
    """Raised whenever an exact-change approval is missing, mismatched,
    expired, or otherwise fails closed. Distinct from StoryNotApproved
    (backlog.py) and ScopeViolation (scope.py) -- all three can apply to
    the same call, and none substitutes for another."""


class ApproverNotAuthorised(ChangeApprovalError):
    """The approver's company roles are not ones the company's approval
    policy allows to approve an exact change."""


def require_supported_operation(capability_id: str, operation: dict) -> dict:
    """The capability must have a complete enforcement contract in the
    catalogue (capability_catalog.require_enforcement), and the operation
    must use the one tool that contract names. Returns the contract.
    Every other capability can be analysed and proposed for Human
    Implementation, but not as an executable change."""
    try:
        enforcement = capability_catalog.require_enforcement(capability_id)
    except capability_catalog.CapabilityError as exc:
        raise ChangeApprovalError(f"capability {capability_id!r} has no execution adapter in Jade: {exc}") from exc
    if operation.get("tool") != enforcement["tool"]:
        raise ChangeApprovalError(
            f"capability {capability_id!r} executes only through {enforcement['tool']!r}, not {operation.get('tool')!r}"
        )
    return enforcement


def _canonical(operation: dict) -> str:
    # Sorted, separator-normalised JSON so the same operation always
    # hashes the same way regardless of key order.
    return json.dumps(operation, sort_keys=True, separators=(",", ":"))


def _hash(operation: dict) -> str:
    return hashlib.sha256(_canonical(operation).encode("utf-8")).hexdigest()


def _path(change_id: str) -> str:
    os.makedirs(CHANGE_DIR, exist_ok=True)
    return os.path.join(CHANGE_DIR, f"{change_id}.json")


def _load(change_id: str) -> Optional[dict]:
    p = _path(change_id)
    if not os.path.exists(p):
        return None
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(change_id: str, record: dict) -> None:
    # Temp file + rename: an interrupted write never leaves a truncated record.
    path = _path(change_id)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


# ---------------------------------------------------------------------
# Propose -- Architect or Functional Agent registers the exact
# operation it intends to execute against an already-approved story.
# This does NOT approve anything; it is the thing a human approves.
# ---------------------------------------------------------------------
def propose_change(story_id: str, operation: dict, capability_id: str, environment: str = "DEV") -> dict:
    """Design update Section 5.2: bind the proposal to its capability
    (and the catalogue/scope revisions in force right now), its exact
    targets, and a DEV-only environment -- BEFORE a human is even asked
    to approve it, not just at execution time. Fails fast on a
    structurally invalid proposal rather than letting a human approve
    something that could never pass require_exact_change anyway.

    capability_id is deliberately a separate parameter, not a key
    inside 'operation': 'operation' is exactly the write payload that
    gets hashed and byte-for-byte compared at execution time (unchanged
    from before this design update, so existing write tools like
    set_processing_option don't need to know about capabilities at all)
    -- capability binding is metadata ABOUT the change, not part of
    what gets written.

    Only the catalogue-only, engagement-independent checks run here
    (capability_id is real; environment is literally "DEV") -- the full
    company-scope-backed environment isolation check and the capability's
    CURRENT executability both belong at require_exact_change instead
    (immediately before the write itself), not here: a change can sit
    pending for a while, and re-approving it against a company scope that
    hasn't even been written yet for a brand-new engagement shouldn't be
    impossible, only executing against JDE without one should be."""
    require_approved(story_id)  # can't propose a change against a story nobody approved
    # The company comes from the story's intake link, never from the
    # caller. An unattributed story cannot even be proposed.
    company_id = company_for_story(story_id)
    cap = capability_catalog.require_capability(capability_id)  # raises CapabilityError if unknown
    require_supported_operation(capability_id, operation)
    if environment != "DEV":
        raise ChangeApprovalError(
            f"'{environment}' is not DEV. All JDE access and execution use "
            "approved DEV endpoints only -- this is a universal rule, not "
            "engagement-configurable."
        )

    change_id = f"{story_id}-CH{int(time.time() * 1000)}"
    record = {
        "change_id": change_id,
        "story_id": story_id,
        "company_id": company_id,
        "operation": operation,
        "change_hash": _hash(operation),
        "environment": environment,
        "capability_id": capability_id,
        "capability_revision": cap["revision"],
        "status": "pending",
        "created_at": time.time(),
        "approved_by": None,
        "approved_at": None,
        "expires_at": None,
        "approver_authority": None,
        "decision_note": None,
        # "Record the versions used for each run" (design update Section
        # 1) -- stamped at propose time so an approver sees exactly which
        # catalogue/scope revisions this proposal was checked against,
        # not whatever happens to be current when someone looks later.
        "catalog_revision": capability_catalog.catalog_revision(),
        "scope_revision": scope_revision(company_id),
    }
    _save(change_id, record)
    return record


# ---------------------------------------------------------------------
# Human-only, exactly like backlog_review.py -- no agent tool wraps
# these. Approving a story and approving a change are two separate
# human decisions, and the second one is about a concrete, readable
# operation, not an abstract request.
# ---------------------------------------------------------------------
def list_pending_changes() -> list[dict]:
    if not os.path.isdir(CHANGE_DIR):
        return []
    out = []
    for fn in sorted(os.listdir(CHANGE_DIR)):
        if fn.endswith(".json") and not fn.startswith("."):  # skip in-flight temp files
            rec = _load(fn[:-5])
            if rec and rec["status"] == "pending":
                out.append(rec)
    return out


def _load_for_company(change_id: str, company_id: str) -> dict:
    record = _load(change_id)
    if record is None:
        raise ChangeApprovalError(f"no such change: {change_id}")
    if not record.get("company_id") or record["company_id"] != company_id:
        # Same answer as a missing record: another company's change is not
        # something this caller can see, let alone decide on.
        raise ChangeApprovalError(f"no such change for company {company_id}: {change_id}")
    return record


def approve_change(
    change_id: str,
    approved_by: str,
    *,
    company_id: str,
    approver_roles: Iterable[str],
    approver_user_id: str,
    note: str = "",
) -> dict:
    """company_id, approver_roles and approver_user_id come from the
    approver's authenticated session (api_service), never from an agent.
    The company's approval policy decides whether those roles may approve
    and how long the approval stays valid; no policy, an unreadable
    policy or no matching role all refuse.

    Runs under the change's lock, so a concurrent approve, reject or
    execution attempt sees either the state before or after, never a mix;
    the approver's roles are re-read from the membership database inside
    the lock, so a role revoked a moment ago cannot approve."""
    from . import execution

    with execution._locked(change_id):
        record = _load_for_company(change_id, company_id)
        if record["status"] != "pending":
            raise ChangeApprovalError(f"change {change_id} is {record['status']}, not pending -- only a pending change can be approved")
        scope = load_company_scope(company_id)
        policy = require_approval_policy(scope)
        held = set(approver_roles)
        matched = sorted(held & set(policy["exact_change_approver_roles"]))
        if not matched:
            raise ApproverNotAuthorised(
                f"{approved_by} does not hold a role this company's approval policy allows to approve an "
                f"exact change (allowed: {', '.join(policy['exact_change_approver_roles'])}; held: "
                f"{', '.join(sorted(held)) or 'none'})."
            )
        try:
            matched = sorted(set(matched) & set(
                authority.require_current_approver(approver_user_id, company_id, policy["exact_change_approver_roles"])
            ))
        except authority.AuthorityRevoked as exc:
            raise ApproverNotAuthorised(str(exc)) from exc
        except authority.AuthorityUnverifiable as exc:
            raise ChangeApprovalError(str(exc)) from exc
        if not matched:
            raise ApproverNotAuthorised(f"{approved_by}'s roles changed while approving -- refusing")
        # What this approval is given against: the design revision, its
        # evidence baseline and artifacts, and the target's before-state.
        from . import binding

        basis = binding.snapshot(record)
        now = time.time()
        record.update({
            "status": "approved",
            "approved_by": approved_by,
            "approved_at": now,
            "expires_at": now + policy["approval_valid_hours"] * 3600,
            "approver_authority": {
                "user_id": approver_user_id,
                "roles": matched,
                "policy_version": policy["policy_version"],
                "scope_revision": str(scope.get("revision", "unknown")),
            },
            "decision_note": note,
            "binding": basis,
        })
        _save(change_id, record)
        return record


def reject_change(change_id: str, approved_by: str, note: str, *, company_id: str) -> dict:
    from . import execution

    if not note:
        raise ChangeApprovalError("a change rejection must include a reason")
    with execution._locked(change_id):
        record = _load_for_company(change_id, company_id)
        if record["status"] != "pending":
            raise ChangeApprovalError(f"change {change_id} is {record['status']}, not pending -- only a pending change can be rejected")
        record.update({"status": "rejected", "approved_by": approved_by, "approved_at": time.time(), "decision_note": note})
        _save(change_id, record)
        return record


def _require_live_approval(change_id: str) -> tuple[dict, dict]:
    """Everything about an approval that must still be true at the
    moment of execution, re-derived from source rather than trusted from
    the record: approved and unexpired; still the same company as the
    story's intake link; that company's scope and approval policy still
    readable; and the approver's recorded roles still allowed by the
    CURRENT policy. Returns (record, company scope)."""
    record = _load(change_id)
    if record is None:
        raise ChangeApprovalError(f"no change record for {change_id} -- propose_change and get it approved first")
    if record["status"] != "approved":
        raise ChangeApprovalError(f"change {change_id} is not approved (status: {record['status']})")
    if not record.get("expires_at"):
        raise ChangeApprovalError(f"change {change_id} has no approval expiry -- re-approve it")
    if time.time() > record["expires_at"]:
        raise ChangeApprovalError(f"change {change_id} approval expired at {record['expires_at']} -- propose it again")
    company_id = company_for_story(record["story_id"])
    if record.get("company_id") != company_id:
        raise ChangeApprovalError(
            f"change {change_id} was recorded for company {record.get('company_id')!r} but its story now "
            f"belongs to {company_id!r} -- refusing."
        )
    scope = load_company_scope(company_id)
    policy = require_approval_policy(scope)
    recorded = record.get("approver_authority") or {}
    if not set(recorded.get("roles") or []) & set(policy["exact_change_approver_roles"]):
        raise ChangeApprovalError(
            f"change {change_id} was not approved by a role the company's current approval policy allows "
            f"({', '.join(policy['exact_change_approver_roles'])}) -- re-approve it."
        )
    # The approver must STILL hold such a role: a demotion, deactivated
    # membership or disabled user after approval leaves nothing usable.
    try:
        authority.require_current_approver(recorded.get("user_id"), company_id, policy["exact_change_approver_roles"])
    except (authority.AuthorityRevoked, authority.AuthorityUnverifiable) as exc:
        raise ChangeApprovalError(f"change {change_id}: {exc}") from exc
    # Defense in depth: re-check the underlying story is still approved too.
    require_approved(record["story_id"])
    return record, scope


# ---------------------------------------------------------------------
# The real control. Every write tool calls this immediately before
# doing anything in JDE.
# ---------------------------------------------------------------------
def require_exact_change(change_id: str, operation: dict) -> dict:
    """Returns the approved record; the caller re-reads the company's
    scope via record["company_id"] for its own scope checks."""
    from . import execution  # imported here: execution builds on this module

    record, scope = _require_live_approval(change_id)
    # An earlier attempt that is in flight, applied, or of unknown outcome
    # blocks this one: no blind retry, no second application.
    execution.require_ready(record, execution.WRITE)
    # The basis the approval was given against must still hold: same design
    # revision, no recorded invalidation, the target still in its before-state.
    from . import binding

    binding.require_valid(record)
    # The write is compared with the approved operation minus its test
    # binding: the test name is enforced separately, by
    # require_change_covers_test, and the write tool never sends it.
    approved_write = {k: v for k, v in record["operation"].items() if k != "test_orchestration"}
    if _canonical(operation) != _canonical(approved_write):
        raise ChangeApprovalError(
            "the operation about to execute does not match the exact change a human approved "
            "-- refusing (fail-closed). This is not a false positive to work around: something "
            "about the operation changed after approval, and that is exactly what this check exists to catch."
        )

    # Design update Section 3/5.2: re-check the capability's CURRENT
    # status and the environment's CURRENT isolation binding
    # immediately before writing -- both can have changed since this
    # change was proposed or even since it was approved. Sourced from
    # the APPROVED RECORD's own capability binding, never from the
    # caller-supplied 'operation'.
    capability_id = record.get("capability_id")
    capability_revision = record.get("capability_revision")
    if not capability_id or not capability_revision:
        raise ChangeApprovalError(
            "approved change record has no capability_id/capability_revision -- "
            "this can only happen to a change proposed before the capability "
            "catalogue existed; re-propose it so it binds to a capability."
        )
    require_supported_operation(capability_id, operation)
    check_environment_binding(scope, record["environment"])
    spike = find_spike_experiment(
        scope,
        capability_id,
        capability_revision,
        operation.get("application", ""),
        operation.get("version", ""),
        operation.get("option", ""),
        record["environment"],
    )
    capability_catalog.require_executable(
        capability_id, capability_revision, record["environment"], spike_experiment_approved=spike is not None
    )
    return record


def require_change_covers_test(change_id: str, test_orchestration_name: str) -> dict:
    """Section 17.1's 'bind the automated test invocation to the
    approved Test Specification rather than a generic story-level test
    permission', implemented pragmatically: the approved change record
    itself names which test verifies it (set when the Functional Agent
    called propose_change), and this checks the test about to run is
    that same one -- not a full separate Test Specification artefact
    and approval flow, which would be more machinery than this pilot's
    one test mechanism (run_orchestration) justifies (Section 18)."""
    from . import execution

    record, _scope = _require_live_approval(change_id)
    # The test verifies the write, so the write must be known to be in JDE,
    # and an earlier test attempt must not be in flight or of unknown outcome.
    if execution.effective_state(record, execution.WRITE) != "applied":
        raise execution.ExecutionBlocked(
            f"change {change_id}: the write is not known to be applied "
            f"(state: {execution.effective_state(record, execution.WRITE)}), so its test cannot run yet"
        )
    execution.require_ready(record, execution.TEST)
    from . import binding

    found = binding.problems(record, read_current=False)  # the write itself changed the target, by design
    if found:
        raise binding.BindingInvalid(f"change {change_id} is not eligible: " + "; ".join(found))
    expected = record["operation"].get("test_orchestration")
    if expected != test_orchestration_name:
        raise ChangeApprovalError(
            f"'{test_orchestration_name}' was not the test named in the approved change "
            f"(expected '{expected}') -- refusing (fail-closed). Running a different test than "
            "the one a human saw approved would defeat the point of binding them together."
        )
    return record


def preflight(change_id: str) -> dict:
    """What the gate would decide for this change right now, check by
    check, without executing anything or recording an attempt. Every
    check that can be evaluated is reported, so a person sees all the
    reasons at once instead of one refusal at a time."""
    from . import backlog, execution, scope as scope_module
    from .ais_client import FSR_SET_PROCESSING_OPTION, require_bound_environment
    from .config import settings

    checks: list[dict] = []

    def check(name: str, fn) -> object:
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 -- reported, never raised
            checks.append({"check": name, "ok": False, "detail": str(exc)})
            return None
        checks.append({"check": name, "ok": True, "detail": ""})
        return result if result is not None else True

    record = _load(change_id)
    if record is None:
        return {"change_id": change_id, "executable": False, "mode": "unknown",
                "checks": [{"check": "Change record exists", "ok": False, "detail": f"no change record {change_id}"}]}
    op = record.get("operation", {})
    check("Story approved (Gate 2)", lambda: backlog.require_approved(record["story_id"]))
    check("Exact change approved, unexpired, same company, approver authority current", lambda: _require_live_approval(change_id))
    # The company's scope is read on its own, so its checks are reported
    # even while the approval itself is still missing.
    scope = check("Company scope saved for the story's company",
                  lambda: load_company_scope(company_for_story(record["story_id"])))
    scope = scope if isinstance(scope, dict) else None
    check("Operation supported for this capability", lambda: require_supported_operation(record.get("capability_id", ""), op))
    if scope is not None:
        check("DEV environment bound and isolation confirmed", lambda: scope_module.check_environment_binding(scope, record["environment"]))
        entry = check("Target is in the company's approved versions",
                      lambda: scope_module.check_functional_scope(scope, op.get("application", ""), op.get("version", ""), op.get("option", "")))
        if isinstance(entry, dict):
            check("Value is one of the allowed values", lambda: scope_module.check_allowed_value(entry, str(op.get("value", ""))))
        enforcement = check("Capability has a complete enforcement contract",
                            lambda: capability_catalog.require_enforcement(record.get("capability_id", "")))
        if isinstance(enforcement, dict):
            check("Mechanism allowed by the company", lambda: scope_module.check_mechanism(scope, enforcement["mechanism"]))
            if isinstance(entry, dict):
                check("Option category declared, not protected, not never-touch",
                      lambda: scope_module.check_option_category(scope, entry, enforcement))
            if op.get("test_orchestration"):
                check("Test is approved, its mechanism allowed, its side effects permitted",
                      lambda: scope_module.check_test_boundary(scope, op["test_orchestration"], enforcement))
        spike = scope_module.find_spike_experiment(
            scope, record.get("capability_id", ""), record.get("capability_revision", ""),
            op.get("application", ""), op.get("version", ""), op.get("option", ""), record["environment"],
        )
        check("Capability executable (validated, or inside a current spike window)",
              lambda: capability_catalog.require_executable(
                  record.get("capability_id", ""), record.get("capability_revision", ""), record["environment"],
                  spike_experiment_approved=spike is not None))
        check("AIS connection points at the bound DEV environment", lambda: require_bound_environment(scope))
    check("Version is not Oracle-owned (XJDE/ZJDE)", lambda: scope_module.reject_if_oracle_owned_version(op.get("version", "")))
    check("No earlier attempt in flight, applied or of unknown outcome", lambda: execution.require_ready(record, execution.WRITE))
    if record.get("status") == "approved":
        from . import binding

        def basis_holds() -> None:
            found = binding.problems(record, read_current=execution.effective_state(record, execution.WRITE) == "ready")
            if found:
                raise binding.BindingInvalid("; ".join(found))

        check("Approval basis still holds (design revision, evidence, no invalidation, target before-state)", basis_holds)
    if not settings.mock_mode:
        def fsr_recorded() -> None:
            if FSR_SET_PROCESSING_OPTION is None:
                raise RuntimeError("FSR_SET_PROCESSING_OPTION is not recorded yet (Experiment A prerequisite A-P5)")

        check("Live write payload (FSR) recorded and reviewed", fsr_recorded)
    return {
        "change_id": change_id,
        "mode": "mock" if settings.mock_mode else "live",
        "executable": all(c["ok"] for c in checks),
        "write_state": execution.effective_state(record, execution.WRITE),
        "test_state": execution.effective_state(record, execution.TEST),
        "checks": checks,
    }
