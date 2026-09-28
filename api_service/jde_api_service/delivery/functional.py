"""
Delivering an approved functional change (a processing-option update) on
the customer's real JD Edwards system.

  1. record_applied  -- a person applied EXACTLY the approved value in DEV.
     Jade re-runs the whole delivery gate, then reads the value back LIVE
     through the customer's connection. Only the approved value is ever
     recorded as applied. Where the connection cannot read it, the person
     states the value they read in JDE, with an evidence reference, and it
     is recorded as stated -- never as a live read.
  2. run_test        -- the approved test orchestration runs LIVE on the
     customer's AIS. The attempt is recorded before the call and settled
     after it; an unclear outcome is unknown and must be reconciled.
     record_test_result -- without an orchestration (or instead of running
     it), the person records the test result against the acceptance
     criteria, with evidence.

Every step appends to the story's tamper-evident evidence chain. Only a
person who holds the Application Manager role right now may record a step.
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

from jde_mcp_server import approval, authority, capability_catalog, execution
from jde_mcp_server.evidence import capture_evidence
from jde_mcp_server.scope import check_test_boundary

from . import live

RECORDER_ROLES = ("product_manager",)


class DeliveryRefused(RuntimeError):
    """Nothing was recorded, with the reason."""


def _require_recorder(company_id: str, actor_user_id: str, actor_name: str) -> None:
    try:
        authority.require_current_approver(actor_user_id, company_id, RECORDER_ROLES)
    except (authority.AuthorityRevoked, authority.AuthorityUnverifiable) as exc:
        raise approval.ApproverNotAuthorised(f"{actor_name} may not record a delivery step: {exc}") from exc


def _target(record: dict) -> str:
    op = record["operation"]
    return f"{op.get('application')}/{op.get('version')}/{op.get('option')}"


def record_applied(change_id: str, *, actor_user_id: str, actor_name: str, evidence_reference: str, note: str = "",
                   stated_value: Optional[str] = None) -> dict[str, Any]:
    record, _scope = approval.authorise_functional_delivery(change_id)
    _require_recorder(record["company_id"], actor_user_id, actor_name)
    op = record["operation"]
    approved = str(op.get("value", ""))
    before = ((record.get("binding") or {}).get("before_state") or {})
    before_value = before.get("value") if before.get("known") else None
    evidence_reference = (evidence_reference or "").strip()
    try:
        observed = live.read_processing_option(record["company_id"], record["story_id"], actor_user_id,
                                               op.get("application", ""), op.get("version", ""), op.get("option", ""))
    except live.LiveUnavailable as exc:
        observed = None
        live_reason = str(exc)
    if observed is not None and observed.found:
        value, source = observed.value, f"live AIS read ({observed.observation_id})"
        reference = evidence_reference or f"live read {observed.observation_id}"
        if value != approved:
            if before_value is not None and value == before_value:
                raise DeliveryRefused(
                    f"JD Edwards still shows {value!r} for {_target(record)} -- the value before the change. Nothing "
                    f"was recorded: apply the approved value {approved!r} in DEV first.")
            raise DeliveryRefused(
                f"JD Edwards shows {value!r} for {_target(record)}, not the approved value {approved!r}. Nothing was "
                "recorded: correct it in DEV to exactly the approved value (a different value needs its own "
                "proposal and approval).")
    else:
        reason = live_reason if observed is None else observed.detail
        if stated_value is None or not evidence_reference:
            raise DeliveryRefused(
                f"Jade cannot read the value back live ({reason}). State the value you read in JDE after applying "
                "it, with an evidence reference (for example a screenshot or ticket).")
        if str(stated_value) != approved:
            raise DeliveryRefused(f"the value you read ({stated_value!r}) is not the approved value {approved!r}; "
                                  "nothing was recorded")
        value, source = str(stated_value), f"stated by {actor_name} (read in JDE); live read unavailable: {reason}"
        reference = evidence_reference
    execution.record(
        change_id, execution.WRITE, "applied", before_value=None if before_value is None else str(before_value),
        revalidate=lambda: approval.authorise_functional_delivery(change_id),
        detail=f"{_target(record)} = {value!r} in DEV ({source})",
        recorded={"by": actor_name, "user_id": actor_user_id, "observed_value": value, "source": source,
                  "evidence_reference": reference, "note": note, "at": time.time()})
    entry = capture_evidence(record["story_id"], {
        "event": "applied_in_dev", "stage": "delivery", "actor": actor_name, "change_id": change_id,
        "detail": f"{_target(record)} set to {value!r} in DEV (before: {before_value!r}); {source}; "
                  f"evidence: {reference}",
        "operation": op, "before_value": before_value, "observed_value": value, "source": source,
        "evidence_reference": reference, "note": note,
    })
    return {"outcome": "applied", "observedValue": value, "beforeValue": before_value, "source": source,
            "evidenceReference": reference, "evidenceEntryHash": entry["entry_hash"]}


def _verification(change_id: str, outcome: dict) -> None:
    with execution._locked(change_id):
        current = approval._load(change_id)
        current["verification"] = outcome
        approval._save(change_id, current)


def run_test(change_id: str, *, actor_user_id: str, actor_name: str) -> dict[str, Any]:
    record = approval._load(change_id)
    if record is None:
        raise DeliveryRefused(f"no change {change_id}")
    name = (record.get("operation") or {}).get("test_orchestration")
    if not name:
        raise DeliveryRefused("the approved change names no test orchestration; record the test result instead")

    def authorise() -> tuple[dict, dict]:
        rec = approval.require_change_covers_test(change_id, name)
        from jde_mcp_server.scope import load_company_scope

        scope = load_company_scope(rec["company_id"])
        test = check_test_boundary(scope, name, capability_catalog.require_enforcement(rec["capability_id"]))
        return rec, test

    record, test = authorise()
    _require_recorder(record["company_id"], actor_user_id, actor_name)
    attempt = execution.begin(change_id, execution.TEST, revalidate=authorise)
    try:
        result = live.run_orchestration(record["company_id"], name, test.get("inputs") or {})
    except live.LiveUnavailable as exc:
        execution.finish(change_id, execution.TEST, attempt, "not_sent", str(exc))
        raise DeliveryRefused(f"the test could not be started: {exc}") from None
    except live.LiveCallFailed as exc:
        execution.finish(change_id, execution.TEST, attempt, "unknown" if exc.sent else "not_sent", str(exc))
        if exc.sent:
            raise DeliveryRefused(f"the test orchestration may have run, but its outcome is unknown ({exc}); check "
                                  "JDE and reconcile the test run") from None
        raise DeliveryRefused(f"the test could not be started: {exc}") from None
    except Exception as exc:  # noqa: BLE001 -- after sending, anything unclean is unknown
        execution.finish(change_id, execution.TEST, attempt, "unknown", f"{type(exc).__name__}: {exc}")
        raise
    execution.finish(change_id, execution.TEST, attempt, "completed", f"orchestration {name} answered")
    answer = json.dumps(result["answer"], default=str)[:4000]
    outcome = {"passed": True, "source": "live orchestration", "orchestration": name, "answer": answer,
               "by": actor_name, "at": time.time()}
    outcome["evidence_entry_hash"] = capture_evidence(record["story_id"], {
        "event": "test_run", "stage": "validation", "actor": actor_name, "change_id": change_id,
        "detail": f"approved test orchestration {name} ran live in {result['environment']} and answered",
        "orchestration": name, "answer": answer})["entry_hash"]
    _verification(change_id, outcome)
    return {"outcome": "completed", "passed": True, "orchestration": name, "answer": result["answer"],
            "evidenceEntryHash": outcome["evidence_entry_hash"]}


def record_test_result(change_id: str, *, actor_user_id: str, actor_name: str, passed: bool,
                       evidence_reference: str, note: str) -> dict[str, Any]:
    if not (note or "").strip() or not (evidence_reference or "").strip():
        raise DeliveryRefused("recording a test result needs what was tested (against the acceptance criteria) "
                              "and an evidence reference")
    record = approval.require_change_covers_test(change_id, None)
    _require_recorder(record["company_id"], actor_user_id, actor_name)
    execution.record(change_id, execution.TEST, "completed",
                     revalidate=lambda: approval.require_change_covers_test(change_id, None),
                     detail=f"test {'passed' if passed else 'FAILED'} (recorded by {actor_name})",
                     recorded={"by": actor_name, "user_id": actor_user_id, "passed": passed,
                               "evidence_reference": evidence_reference.strip(), "note": note.strip()})
    outcome = {"passed": bool(passed), "source": "recorded by a person", "by": actor_name, "at": time.time(),
               "evidence_reference": evidence_reference.strip(), "note": note.strip()}
    outcome["evidence_entry_hash"] = capture_evidence(record["story_id"], {
        "event": "test_result_recorded", "stage": "validation", "actor": actor_name, "change_id": change_id,
        "detail": f"test {'PASSED' if passed else 'FAILED'} in DEV, recorded by {actor_name}: {note.strip()} "
                  f"(evidence: {evidence_reference.strip()})"})["entry_hash"]
    _verification(change_id, outcome)
    return {"outcome": "completed", "passed": bool(passed), "evidenceEntryHash": outcome["evidence_entry_hash"]}
