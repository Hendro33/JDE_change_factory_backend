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
from datetime import datetime, timezone
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


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _exact_target(record: dict, kind: str) -> dict:
    """What was checked, identified exactly: the company, story, change and
    capability, the JDE environment the company's scope binds, and for a
    write the application/version/option and approved value, for a test
    the orchestration."""
    op = record.get("operation") or {}
    try:
        env = (approval.load_company_scope(record.get("company_id") or "").get("environment") or {})
        jde_environment = env.get("dev_environment_id") or None
    except Exception:  # noqa: BLE001 -- recorded as unknown, never guessed
        jde_environment = None
    target = {
        "company_id": record.get("company_id"),
        "story_id": record.get("story_id"),
        "change_id": record.get("change_id"),
        "capability_id": record.get("capability_id"),
        "capability_revision": record.get("capability_revision"),
        "environment": record.get("environment"),
        "jde_environment": jde_environment,
    }
    if kind == WRITE:
        target.update({
            "application": op.get("application"), "version": op.get("version"), "option": op.get("option"),
            "approved_value": str(op.get("value", "")),
        })
    else:
        target["orchestration"] = op.get("test_orchestration")
    return target


def _audit(record: dict, kind: str, block: dict, *, observed: dict, outcome: str, actor_user_id: str,
           actor_name: str, source: str, evidence_reference: str, note: str) -> dict:
    """One reconciliation, recorded on the change AND appended to the
    story's tamper-evident evidence chain."""
    from .evidence import capture_evidence

    if not actor_user_id:
        raise approval.ChangeApprovalError("a reconciliation must record who performed it")
    if not (evidence_reference or "").strip():
        raise approval.ChangeApprovalError(
            "a reconciliation needs an evidence reference: where the observed state can be checked "
            "(e.g. a screenshot or ticket reference, or the automated read)"
        )
    unknown_attempt = next((a for a in reversed(block.get("attempts") or []) if a.get("outcome") in (None, "unknown")), None)
    now = _now()
    entry = {
        "kind": f"{kind}_reconciliation",
        "at": now,
        "at_iso": _iso(now),
        "actor": {"user_id": actor_user_id, "display_name": actor_name},
        "verified_by": actor_name,
        "target": _exact_target(record, kind),
        "observed": observed,
        "source": source,
        "evidence_reference": evidence_reference.strip(),
        "settles_attempt_id": unknown_attempt.get("attempt_id") if unknown_attempt else None,
        "outcome": outcome,
        "note": note,
    }
    target = entry["target"]
    where = (
        f"{target.get('application')}/{target.get('version')}/{target.get('option')}" if kind == WRITE
        else f"test {target.get('orchestration')}"
    )
    chained = capture_evidence(record["story_id"], {
        "event": entry["kind"],
        "stage": entry["kind"],
        "actor": actor_name,
        "detail": f"{where} in {target.get('jde_environment') or 'unbound environment'}: {outcome} "
                  f"(observed {observed}; {source}; evidence: {entry['evidence_reference']})",
        "reconciliation": entry,
    })
    entry["evidence_entry_hash"] = chained["entry_hash"]
    block["reconciliations"].append(entry)
    return entry


def reconcile_write(
    change_id: str, *, observed_value: str, source: str, actor_user_id: str, actor_name: str,
    evidence_reference: str, note: str = "",
) -> dict:
    """Compare the ACTUAL target value with the approved and before values
    and record the reconciliation as an audited action. Outcome: applied,
    not_applied (ready again -- a retry still passes every normal check:
    approval expiry, current scope, current policy and approver authority),
    or diverged. Only a WRITE of unknown outcome is reconciled here; a test
    run is reconciled separately (reconcile_test)."""
    with _locked(change_id):
        record = approval._load(change_id)
        if record is None:
            raise approval.ChangeApprovalError(f"no change record for {change_id}")
        state = effective_state(record, WRITE)
        if state not in ("unknown",):
            raise ExecutionBlocked(f"change {change_id}: the write is {state}; only a write of unknown outcome is reconciled")
        block = _block(record, WRITE)
        approved_value = str(record["operation"].get("value", ""))
        before = next((a.get("before_value") for a in reversed(block["attempts"]) if a.get("before_value") is not None), None)
        if observed_value == approved_value:
            outcome, new_state = "applied", "applied"
        elif before is not None and observed_value == before:
            outcome, new_state = "not_applied", "ready"
        else:
            outcome, new_state = "diverged", "diverged"
        entry = _audit(
            record, WRITE, block, observed={"value": observed_value, "before_value": before}, outcome=outcome,
            actor_user_id=actor_user_id, actor_name=actor_name, source=source,
            evidence_reference=evidence_reference, note=note,
        )
        entry["observed_value"] = observed_value  # flat copy for older readers
        block["state"] = new_state
        approval._save(change_id, record)
        return entry


def reconcile_test(
    change_id: str, *, ran: bool, actor_user_id: str, actor_name: str, evidence_reference: str, note: str
) -> dict:
    """A test run's effects cannot be read back generically, so a person
    who checked JDE attests whether it ran, with a note and an evidence
    reference. Only a TEST of unknown outcome is reconciled here -- never
    the write."""
    if not note.strip():
        raise approval.ChangeApprovalError("reconciling a test run needs a note saying what was checked in JDE")
    with _locked(change_id):
        record = approval._load(change_id)
        if record is None:
            raise approval.ChangeApprovalError(f"no change record for {change_id}")
        state = effective_state(record, TEST)
        if state != "unknown":
            raise ExecutionBlocked(f"change {change_id}: the test is {state}; only a test of unknown outcome is reconciled")
        block = _block(record, TEST)
        outcome = "completed" if ran else "not_run"
        entry = _audit(
            record, TEST, block, observed={"ran": ran}, outcome=outcome, actor_user_id=actor_user_id,
            actor_name=actor_name, source="human-verified in JDE", evidence_reference=evidence_reference, note=note,
        )
        block["state"] = "completed" if ran else "ready"
        approval._save(change_id, record)
        return entry
