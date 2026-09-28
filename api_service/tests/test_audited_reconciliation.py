"""
Reconciliation is an audited action, and "not applied" is not a bypass.

  * Every reconciliation records the exact target (company, story, change,
    capability, environment and bound JDE environment, application/
    version/option and approved value -- or the orchestration for a test),
    the observed state, the actor (user id and name), the time, and an
    evidence reference; and it is appended to the story's tamper-evident
    evidence chain.
  * A write reconciled as "not applied" becomes ready again, but a retry
    still passes every normal precondition: approval expiry, the company's
    CURRENT scope, its CURRENT approval policy and the approver's CURRENT
    authority. Reconciling never extends or refreshes an approval.
  * Write reconciliation and test reconciliation are separate actions on
    separate records; neither can settle or alter the other.

A write of unknown outcome only exists in records written before the
recorded delivery route (an automated write interrupted after sending);
the recorded route never produces one. The tests recreate that persisted
state with execution.begin()/finish() (see _legacy_unknown_write), and a
"not applied" retry is the recorded route (a person applies the value in
DEV and records it), which re-runs the whole delivery gate.
"""

from __future__ import annotations

import datetime as dt

import pytest

from .conftest import headers
from .test_concurrency_and_stale_authority import _set_roles
from .test_execution_attempts import _legacy_unknown_write, _ready_change, _record, _state
from .test_stage1_execution_safeguards import _edit_change_record, _execute, _full_scope, _save_scope


def _unknown_write(client, story: str, *, reached_jde: bool, connected: bool = False) -> dict:
    """An approved change left with a write of unknown outcome (the state an
    older interrupted automated write persisted). reached_jde: whether the
    approved value is actually in DEV (on the fake AIS server)."""
    from .fixtures.fake_ais import apply_in_dev

    change = _ready_change(client, story, connected=connected)
    _legacy_unknown_write(change["change_id"], before_value="S3")
    if reached_jde:
        apply_in_dev("vdb", "P4210", "CIQ0001", "PDOCTYPE", "SO")
    assert _state(change["change_id"]) == "unknown"
    return change


def test_a_write_reconciliation_records_target_observation_actor_time_and_evidence(client):
    from jde_mcp_server.evidence import verify_chain

    change = _unknown_write(client, "S-AR-AUDIT", reached_jde=True, connected=True)
    attempt_id = _record(change["change_id"])["execution"]["write"]["attempts"][-1]["attempt_id"]
    r = client.post("/changes/S-AR-AUDIT/execution/reconcile", headers=headers("vdb"), json={"note": "checked"})
    assert r.status_code == 200, r.text

    rec = client.get("/changes/S-AR-AUDIT", headers=headers("vdb")).json()["exactChange"]["execution"]["writeReconciliations"][0]
    assert rec["kind"] == "write_reconciliation"
    assert rec["target"] == {
        "company_id": "vdb", "story_id": "S-AR-AUDIT", "change_id": change["change_id"],
        "capability_id": "processing_option_update", "capability_revision": "r1",
        "environment": "DEV", "jde_environment": "JDV920",
        "application": "P4210", "version": "CIQ0001", "option": "PDOCTYPE", "approved_value": "SO",
    }
    # Read live through the company's connection; the before value is the unknown attempt's.
    assert rec["observed"] == {"value": "SO", "before_value": "S3"}
    assert rec["source"].startswith("live AIS read (")
    assert rec["actor"] == {"userId": "u-hendro", "displayName": "Hendro"}
    assert dt.datetime.fromisoformat(rec["at"]).tzinfo is not None
    assert rec["evidenceReference"].startswith("live read ") and "of P4210/CIQ0001/PDOCTYPE" in rec["evidenceReference"]
    assert rec["settlesAttemptId"] == attempt_id
    assert rec["outcome"] == "applied"

    # The same record is in the tamper-evident chain, and the chain verifies.
    chain = verify_chain("S-AR-AUDIT")
    assert chain["valid"] is True
    from jde_mcp_server import evidence as mcp_evidence

    entries = mcp_evidence.entries("S-AR-AUDIT")
    assert entries[-1]["event"] == "write_reconciliation"
    assert entries[-1]["entry_hash"] == rec["evidenceEntryHash"]
    assert entries[-1]["reconciliation"]["actor"]["user_id"] == "u-hendro"
    assert entries[-1]["reconciliation"]["target"]["approved_value"] == "SO"


def test_a_person_stated_value_needs_an_evidence_reference(client):
    from jde_mcp_server import execution
    from jde_mcp_server.approval import ChangeApprovalError

    change = _unknown_write(client, "S-AR-EVID", reached_jde=True)
    for kwargs in ({"evidence_reference": "  ", "actor_user_id": "u-hendro"}, {"evidence_reference": "JIRA-1", "actor_user_id": ""}):
        with pytest.raises(ChangeApprovalError):
            execution.reconcile_write(
                change["change_id"], observed_value="SO", source="human-verified in JDE", actor_name="Hendro", **kwargs
            )
    assert _state(change["change_id"]) == "unknown"
    assert _record(change["change_id"])["execution"]["write"]["reconciliations"] == []


# ---------------------------------------------------------------------
# "Not applied" keeps every precondition
# ---------------------------------------------------------------------
def _not_applied(client, story: str) -> dict:
    change = _unknown_write(client, story, reached_jde=False)
    r = client.post(f"/changes/{story}/execution/reconcile", headers=headers("vdb"),
                    json={"observedValue": "S3", "note": "not there: P983051 still shows S3", "evidenceReference": "JIRA-5"})
    assert r.json()["outcome"] == "not_applied" and _state(change["change_id"]) == "ready"
    return change


def test_not_applied_does_not_refresh_the_approval_expiry(client):
    from jde_mcp_server.approval import ChangeApprovalError

    change = _not_applied(client, "S-AR-EXPIRED")
    before = _record(change["change_id"])["expires_at"]
    _edit_change_record(change["change_id"], expires_at=1.0)  # the approval lapsed while this was being investigated
    with pytest.raises(ChangeApprovalError, match="expired"):
        _execute("S-AR-EXPIRED", change["change_id"])
    assert before > 1.0 and _state(change["change_id"]) == "ready"


def test_not_applied_retry_uses_the_current_scope(client):
    from jde_mcp_server.scope import ScopeViolation

    change = _not_applied(client, "S-AR-SCOPE")
    scope = _full_scope()
    scope["functionalAgent"]["approvedVersions"] = []  # target withdrawn from scope
    _save_scope(client, "vdb", scope)
    with pytest.raises(ScopeViolation):
        _execute("S-AR-SCOPE", change["change_id"])


def test_not_applied_retry_uses_the_current_policy(client):
    from jde_mcp_server.approval import ChangeApprovalError

    change = _not_applied(client, "S-AR-POLICY")
    _save_scope(client, "vdb", _full_scope(policy={"policyVersion": 1, "exactChangeApproverRoles": ["admin"], "approvalValidHours": 24}))
    with pytest.raises(ChangeApprovalError, match="current approval policy"):
        _execute("S-AR-POLICY", change["change_id"])


def test_not_applied_retry_uses_the_approvers_current_authority(client):
    from jde_mcp_server.approval import ChangeApprovalError

    change = _not_applied(client, "S-AR-AUTH")
    _set_roles("vdb", "u-hendro", ["admin"])
    with pytest.raises(ChangeApprovalError, match="no longer holds"):
        _execute("S-AR-AUTH", change["change_id"])


def test_reconciliation_itself_needs_a_current_policy_approver(client, viewer_client):
    _unknown_write(client, "S-AR-WHO", reached_jde=True)
    r = viewer_client.post("/changes/S-AR-WHO/execution/reconcile", headers=headers("vdb"),
                           json={"observedValue": "SO", "note": "x", "evidenceReference": "JIRA-6"})
    assert r.status_code == 403


# ---------------------------------------------------------------------
# Write and test reconciliation are separate
# ---------------------------------------------------------------------
def test_a_test_reconciliation_cannot_settle_an_unknown_write(client):
    change = _unknown_write(client, "S-AR-CROSS1", reached_jde=True)
    r = client.post(
        "/changes/S-AR-CROSS1/execution/reconcile-test", headers=headers("vdb"),
        json={"ran": True, "note": "looked", "evidenceReference": "JIRA-9"},
    )
    assert r.status_code == 409
    assert _state(change["change_id"]) == "unknown"


def test_a_write_reconciliation_cannot_settle_an_unknown_test(client):
    from jde_mcp_server import execution

    change = _ready_change(client, "S-AR-CROSS2")
    _execute("S-AR-CROSS2", change["change_id"])
    # A live test orchestration that timed out after sending.
    attempt = execution.begin(change["change_id"], execution.TEST)
    execution.finish(change["change_id"], execution.TEST, attempt, "unknown", "orchestrator timed out")

    r = client.post("/changes/S-AR-CROSS2/execution/reconcile", headers=headers("vdb"),
                    json={"observedValue": "SO", "note": "x", "evidenceReference": "JIRA-11"})
    assert r.status_code == 409
    assert _state(change["change_id"], "test") == "unknown"

    r = client.post(
        "/changes/S-AR-CROSS2/execution/reconcile-test", headers=headers("vdb"),
        json={"ran": True, "note": "order 1234 exists in DEV", "evidenceReference": "screenshot JIRA-10"},
    )
    assert r.status_code == 200 and r.json()["outcome"] == "completed"
    assert "orchestration" in r.json()["target"] and "approved_value" not in r.json()["target"]
    execution_status = client.get("/changes/S-AR-CROSS2", headers=headers("vdb")).json()["exactChange"]["execution"]
    assert execution_status["writeReconciliations"] == []
    assert [t["kind"] for t in execution_status["testReconciliations"]] == ["test_reconciliation"]
    assert execution_status["testReconciliations"][0]["observed"] == {"ran": True}
    assert execution_status["writeState"] == "applied" and execution_status["testState"] == "completed"
