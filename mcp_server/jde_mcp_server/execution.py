"""
Execution attempts and reconciliation for an approved exact change.

An approval says a write MAY happen once. This module records whether it
DID. The distinction matters because a write can be interrupted after
the request left for JDE -- a timeout, a dropped connection, a crashed
process -- and then nobody knows whether JDE applied it. Retrying blindly
could apply it twice or on top of something else; assuming it failed
could leave an unrecorded change in DEV.

So every write and every test run is recorded as an attempt BEFORE the
request is sent, and closed with what is actually known afterwards:

  ready        -- nothing in flight; the gate may allow an attempt
  in_progress  -- an attempt started and has not finished
  applied      -- the write is known to be in JDE (write only)
  completed    -- the test run finished (test only)
  unknown      -- an attempt may or may not have reached JDE
  diverged     -- reconciliation found JDE in neither the before nor the
                  approved state; this change can never execute again

Only `ready` lets the gate proceed. `unknown` (and an `in_progress` that
is older than STALE_AFTER_SECONDS, or left behind by a restart) blocks
until someone with approval authority reconciles it by checking the
actual target state. A write that is `applied` never runs again under
the same change: a further change needs a new proposal and approval.

The record lives in the change record itself (JDE_CHANGE_DIR), so it
survives restarts. A file lock per change serialises processes (the API
and any MCP server process started for an agent run).
"""

from __future__ import annotations

import fcntl
import os
import time
import uuid
from contextlib import contextmanager
from typing import Callable, Iterator, Optional

from . import approval

STALE_AFTER_SECONDS = 15 * 60

WRITE = "write"
TEST = "test"


class ExecutionBlocked(approval.ChangeApprovalError):
    """The change's execution state does not allow another attempt."""


@contextmanager
def _locked(change_id: str) -> Iterator[None]:
    os.makedirs(approval.CHANGE_DIR, exist_ok=True)
    path = os.path.join(approval.CHANGE_DIR, f".{change_id}.lock")
    with open(path, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _now() -> float:
    return time.time()


def _block(record: dict, kind: str) -> dict:
    execution = record.setdefault("execution", {})
    return execution.setdefault(kind, {"state": "ready", "attempts": [], "reconciliations": []})


def effective_state(record: dict, kind: str = WRITE) -> str:
    block = (record.get("execution") or {}).get(kind)
    if not block:
        return "ready"
    state = block.get("state", "ready")
    if state == "in_progress":
        started = (block.get("attempts") or [{}])[-1].get("started_at") or 0
        if _now() - started > STALE_AFTER_SECONDS:
            return "unknown"
    return state


_BLOCK_REASON = {
    "in_progress": "an attempt is already in progress for this change",
    "applied": "this change has already been applied in JDE; a further change needs a new proposal and approval",
    "completed": "the test for this change has already run; running it again needs a new proposal and approval",
    "unknown": "the outcome of an earlier attempt is unknown -- it may or may not have reached JDE. "
               "Reconcile it by checking the actual target state before anything else runs",
    "diverged": "reconciliation found the target in neither the before nor the approved state; "
                "this change can no longer execute -- investigate, then propose a new change",
}


def require_ready(record: dict, kind: str) -> None:
    state = effective_state(record, kind)
    if state != "ready":
        raise ExecutionBlocked(f"change {record['change_id']}: {_BLOCK_REASON.get(state, state)} (state: {state})")


def begin(
    change_id: str, kind: str, *, before_value: Optional[str] = None, revalidate: Optional[Callable[[], object]] = None
) -> str:
    """Record an attempt BEFORE the request is sent. Raises ExecutionBlocked
    unless the change is ready -- this is what stops a blind retry.

    `revalidate` re-runs every authorisation and eligibility check (the
    approval, its expiry, the approver's CURRENT roles, the company's scope
    and capability boundaries) inside the same lock that approve, reject,
    reconcile and other attempts take -- so nothing that changes between
    the caller's own checks and this moment can be used stale."""
    with _locked(change_id):
        if revalidate is not None:
            revalidate()
        record = approval._load(change_id)
        if record is None:
            raise approval.ChangeApprovalError(f"no change record for {change_id}")
        require_ready(record, kind)
        block = _block(record, kind)
        attempt_id = uuid.uuid4().hex[:12]
        block["attempts"].append({
            "attempt_id": attempt_id,
            "started_at": _now(),
            "finished_at": None,
            "before_value": before_value,
            "outcome": None,
            "detail": "",
        })
        block["state"] = "in_progress"
        approval._save(change_id, record)
        return attempt_id


_OUTCOME_STATE = {
    (WRITE, "applied"): "applied",
    (TEST, "completed"): "completed",
    (WRITE, "not_sent"): "ready",
    (TEST, "not_sent"): "ready",
    (WRITE, "unknown"): "unknown",
    (TEST, "unknown"): "unknown",
}


def finish(change_id: str, kind: str, attempt_id: str, outcome: str, detail: str = "") -> None:
    """Close an attempt. `not_sent` is only for failures that provably
    happened before the request left (e.g. a refused connection); anything
    after sending that is not a clean success is `unknown`."""
    with _locked(change_id):
        record = approval._load(change_id)
        block = _block(record, kind)
        attempt = next((a for a in block["attempts"] if a["attempt_id"] == attempt_id), None)
        if attempt is None:
            raise approval.ChangeApprovalError(f"no attempt {attempt_id} on change {change_id}")
        attempt.update({"finished_at": _now(), "outcome": outcome, "detail": detail[:500]})
        is_current = block["state"] == "in_progress" and block["attempts"][-1]["attempt_id"] == attempt_id
        if is_current:
            block["state"] = _OUTCOME_STATE[(kind, outcome)]
        else:
            # Something else (e.g. restart recovery) already declared this
            # attempt's fate. Keep what we learned, but stay conservative:
            # only a reconciliation moves it on.
            block["state"] = "unknown" if block["state"] != "diverged" else "diverged"
        approval._save(change_id, record)


def mark_interrupted_unknown() -> int:
    """At startup nothing can still be running in this process, so every
    in-progress attempt is recorded as unknown. Returns how many."""
    if not os.path.isdir(approval.CHANGE_DIR):
        return 0
    count = 0
    for fn in sorted(os.listdir(approval.CHANGE_DIR)):
        if not fn.endswith(".json") or fn.startswith("."):
            continue
        change_id = fn[:-5]
        with _locked(change_id):
            record = approval._load(change_id)
            changed = False
            for kind in (WRITE, TEST):
                block = (record.get("execution") or {}).get(kind)
                if block and block.get("state") == "in_progress":
                    block["state"] = "unknown"
                    last = block["attempts"][-1]
                    last["detail"] = (last.get("detail") or "") + " Interrupted by a restart before its outcome was recorded."
                    changed = True
                    count += 1
            if changed:
                approval._save(change_id, record)
    return count


def reconcile_write(
    change_id: str, *, observed_value: str, source: str, verified_by: str, note: str = ""
) -> str:
    """Compare the ACTUAL target value with the approved and before values.
    Returns the outcome: applied, not_applied (ready again) or diverged."""
    with _locked(change_id):
        record = approval._load(change_id)
        if record is None:
            raise approval.ChangeApprovalError(f"no change record for {change_id}")
        state = effective_state(record, WRITE)
        if state not in ("unknown",):
            raise ExecutionBlocked(f"change {change_id} is {state}; only an unknown outcome is reconciled")
        block = _block(record, WRITE)
        approved_value = str(record["operation"].get("value", ""))
        before = next((a.get("before_value") for a in reversed(block["attempts"]) if a.get("before_value") is not None), None)
        if observed_value == approved_value:
            outcome, new_state = "applied", "applied"
        elif before is not None and observed_value == before:
            outcome, new_state = "not_applied", "ready"
        else:
            outcome, new_state = "diverged", "diverged"
        block["reconciliations"].append({
            "at": _now(), "verified_by": verified_by, "source": source, "observed_value": observed_value,
            "approved_value": approved_value, "before_value": before, "outcome": outcome, "note": note,
        })
        block["state"] = new_state
        approval._save(change_id, record)
        return outcome


def reconcile_test(change_id: str, *, ran: bool, verified_by: str, note: str) -> str:
    """A test run's effects cannot be read back generically, so a person
    who checked JDE attests whether it ran. A reason is required."""
    if not note.strip():
        raise approval.ChangeApprovalError("reconciling a test run needs a note saying what was checked in JDE")
    with _locked(change_id):
        record = approval._load(change_id)
        if effective_state(record, TEST) != "unknown":
            raise ExecutionBlocked(f"change {change_id}: the test outcome is not unknown; nothing to reconcile")
        block = _block(record, TEST)
        outcome = "completed" if ran else "not_run"
        block["reconciliations"].append({
            "at": _now(), "verified_by": verified_by, "source": "human-verified in JDE", "outcome": outcome, "note": note,
        })
        block["state"] = "completed" if ran else "ready"
        approval._save(change_id, record)
        return outcome
