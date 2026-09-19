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
import time
from typing import Optional

from .backlog import require_approved, BacklogError

CHANGE_DIR = os.environ.get("JDE_CHANGE_DIR", "./changes")
DEFAULT_EXPIRY_SECONDS = int(os.environ.get("JDE_CHANGE_APPROVAL_EXPIRY_SECONDS", str(24 * 3600)))


class ChangeApprovalError(RuntimeError):
    """Raised whenever an exact-change approval is missing, mismatched,
    expired, or otherwise fails closed. Distinct from StoryNotApproved
    (backlog.py) and ScopeViolation (scope.py) -- all three can apply to
    the same call, and none substitutes for another."""


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
    with open(_path(change_id), "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2)


# ---------------------------------------------------------------------
# Propose -- Architect or Functional Agent registers the exact
# operation it intends to execute against an already-approved story.
# This does NOT approve anything; it is the thing a human approves.
# ---------------------------------------------------------------------
def propose_change(story_id: str, operation: dict, environment: str = "DEV") -> dict:
    require_approved(story_id)  # can't propose a change against a story nobody approved
    change_id = f"{story_id}-CH{int(time.time() * 1000)}"
    record = {
        "change_id": change_id,
        "story_id": story_id,
        "operation": operation,
        "change_hash": _hash(operation),
        "environment": environment,
        "status": "pending",
        "created_at": time.time(),
        "approved_by": None,
        "approved_at": None,
        "expires_at": None,
        "decision_note": None,
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
        if fn.endswith(".json"):
            rec = _load(fn[:-5])
            if rec and rec["status"] == "pending":
                out.append(rec)
    return out


def approve_change(change_id: str, approved_by: str, expiry_seconds: int = DEFAULT_EXPIRY_SECONDS, note: str = "") -> dict:
    record = _load(change_id)
    if record is None:
        raise ChangeApprovalError(f"no such change: {change_id}")
    record.update({
        "status": "approved",
        "approved_by": approved_by,
        "approved_at": time.time(),
        "expires_at": time.time() + expiry_seconds,
        "decision_note": note,
    })
    _save(change_id, record)
    return record


def reject_change(change_id: str, approved_by: str, note: str) -> dict:
    if not note:
        raise ChangeApprovalError("a change rejection must include a reason")
    record = _load(change_id)
    if record is None:
        raise ChangeApprovalError(f"no such change: {change_id}")
    record.update({"status": "rejected", "approved_by": approved_by, "approved_at": time.time(), "decision_note": note})
    _save(change_id, record)
    return record


# ---------------------------------------------------------------------
# The real control. Every write tool calls this immediately before
# doing anything in JDE.
# ---------------------------------------------------------------------
def require_exact_change(change_id: str, operation: dict) -> dict:
    record = _load(change_id)
    if record is None:
        raise ChangeApprovalError(f"no change record for {change_id} -- propose_change and get it approved first")
    if record["status"] != "approved":
        raise ChangeApprovalError(f"change {change_id} is not approved (status: {record['status']})")
    if record["expires_at"] and time.time() > record["expires_at"]:
        raise ChangeApprovalError(f"change {change_id} approval expired at {record['expires_at']} -- propose it again")
    if _hash(operation) != record["change_hash"]:
        raise ChangeApprovalError(
            "the operation about to execute does not match the exact change a human approved "
            "-- refusing (fail-closed). This is not a false positive to work around: something "
            "about the operation changed after approval, and that is exactly what this check exists to catch."
        )
    # Defense in depth: re-check the underlying story is still approved too,
    # not just that the change record says so.
    require_approved(record["story_id"])
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
    record = _load(change_id)
    if record is None:
        raise ChangeApprovalError(f"no change record for {change_id}")
    if record["status"] != "approved":
        raise ChangeApprovalError(f"change {change_id} is not approved (status: {record['status']})")
    expected = record["operation"].get("test_orchestration")
    if expected != test_orchestration_name:
        raise ChangeApprovalError(
            f"'{test_orchestration_name}' was not the test named in the approved change "
            f"(expected '{expected}') -- refusing (fail-closed). Running a different test than "
            "the one a human saw approved would defeat the point of binding them together."
        )
    require_approved(record["story_id"])
    return record
