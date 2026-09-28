"""
Interrupted and repeated execution (mcp_server/jde_mcp_server/execution.py).

Delivery is the recorded route (delivery/functional.py): a person applies
the approved value in DEV and records it. A recorded write is "applied" in
one locked step -- it is never "unknown" -- and it never runs twice under
the same approval. The live test orchestration is still recorded as an
attempt BEFORE it is sent, so an interrupted call is "unknown", any retry is
refused, and only a reconciliation lets anything run again.

A write of unknown outcome can only come from records written before the
recorded route (an automated write interrupted after sending). The tests
below recreate exactly that persisted state with execution.begin()/finish()
and check that reconciliation against the ACTUAL target value -- read live
through the company's JDE connection (the fake AIS server), or stated by a
person with a note and evidence -- is what settles it.
"""

from __future__ import annotations

import time

import pytest

from .conftest import headers
from .test_stage1_execution_safeguards import (
    OPERATION,
    POLICY,
    _approve,
    _approved_story,
    _execute,
    _full_scope,
    _propose,
    _record_test,
    _save_scope,
)


def _ready_change(client, story_id: str, *, connected: bool = False) -> dict:
    """An approved change. connected=True first readies the company's JDE
    connection (the fake AIS server), so Jade can read values live."""
    if connected:
        from ._discovery import ready_company

        ready_company(client, "vdb")
    _save_scope(client, "vdb", _full_scope())
    _approved_story(story_id)
    change = _propose(story_id)
    _approve(change["change_id"])
    return change


def _record(change_id: str) -> dict:
    from jde_mcp_server import approval

    return approval._load(change_id)


def _state(change_id: str, kind: str = "write") -> str:
    from jde_mcp_server import execution

    return execution.effective_state(_record(change_id), kind)


def _legacy_unknown_write(change_id: str, before_value: str = "S3") -> None:
    """The persisted state an OLDER automated write left behind when it was
    interrupted after the request was sent: an attempt of unknown outcome.
    The recorded route can never produce this itself; it is put in place
    directly so the reconciliation of such records stays tested."""
    from jde_mcp_server import execution

    attempt = execution.begin(change_id, execution.WRITE, before_value=before_value)
    execution.finish(change_id, execution.WRITE, attempt, "unknown", "no response after sending (older automated write)")


def _record_applied(change_id: str, stated: str = "SO"):
    """Record the step WITHOUT touching DEV (unlike _execute)."""
    from jde_api_service.delivery import functional

    return functional.record_applied(change_id, actor_user_id="u-hendro", actor_name="Hendro",
                                     evidence_reference="screenshot TEST-1", stated_value=stated)


# ---------------------------------------------------------------------
# One approval, one application
# ---------------------------------------------------------------------
def test_an_applied_change_never_runs_twice(client):
    from jde_mcp_server.execution import ExecutionBlocked

    change = _ready_change(client, "S-EX-ONCE")
    result = _execute("S-EX-ONCE", change["change_id"])
    # No connection is ready, so the person's stated value is recorded, labelled as such.
    assert result["outcome"] == "applied" and result["observedValue"] == "SO"
    assert result["source"].startswith("stated by Hendro (read in JDE)")
    assert _state(change["change_id"]) == "applied"
    with pytest.raises(ExecutionBlocked, match="already been applied"):
        _execute("S-EX-ONCE", change["change_id"])
    assert len(_record(change["change_id"])["execution"]["write"]["attempts"]) == 1


def test_a_recorded_write_is_applied_never_unknown_and_is_not_reconciled(client):
    from fastapi.testclient import TestClient
    from jde_api_service.main import app

    change = _ready_change(client, "S-EX-RECORDED")
    _execute("S-EX-RECORDED", change["change_id"])
    attempt = _record(change["change_id"])["execution"]["write"]["attempts"][-1]
    assert attempt["outcome"] == "applied" and attempt["finished_at"] == attempt["started_at"]
    assert attempt["recorded"]["evidence_reference"] == "screenshot TEST-1 of P4210|CIQ0001"
    with TestClient(app):  # a restart cannot turn a recorded step into an unknown one
        pass
    assert _state(change["change_id"]) == "applied"
    r = client.post("/changes/S-EX-RECORDED/execution/reconcile", headers=headers("vdb"),
                    json={"observedValue": "SO", "note": "checked", "evidenceReference": "x"})
    assert r.status_code == 409 and "only a write of unknown outcome is reconciled" in r.json()["detail"]


def test_without_a_live_read_a_stated_value_needs_evidence_and_must_be_the_approved_value(client):
    from jde_api_service.delivery import functional

    change = _ready_change(client, "S-EX-STATED")
    with pytest.raises(functional.DeliveryRefused, match="State the value you read in JDE"):
        functional.record_applied(change["change_id"], actor_user_id="u-hendro", actor_name="Hendro",
                                  evidence_reference="", stated_value="SO")
    with pytest.raises(functional.DeliveryRefused, match="not the approved value"):
        _record_applied(change["change_id"], stated="SQ")
    assert _state(change["change_id"]) == "ready"
    assert not (_record(change["change_id"]).get("execution") or {}).get("write", {}).get("attempts")


# ---------------------------------------------------------------------
# A write of unknown outcome (older records): blocked until reconciled
# ---------------------------------------------------------------------
def test_an_unknown_write_that_reached_jde_is_reconciled_live_as_applied(client):
    from jde_mcp_server.execution import ExecutionBlocked

    from .fixtures.fake_ais import apply_in_dev

    change = _ready_change(client, "S-EX-TIMEOUT", connected=True)
    _legacy_unknown_write(change["change_id"])
    apply_in_dev("vdb", "P4210", "CIQ0001", "PDOCTYPE", "SO")  # the interrupted write did reach JDE
    assert _state(change["change_id"]) == "unknown"

    # No recording on top of an unknown outcome, even with the approved value in DEV.
    with pytest.raises(ExecutionBlocked, match="unknown"):
        _record_applied(change["change_id"])

    # The preflight says why, among its checks.
    pre = client.get("/changes/S-EX-TIMEOUT/execution/preflight", headers=headers("vdb")).json()
    assert pre["executable"] is False and pre["writeState"] == "unknown"
    assert any(not c["ok"] and "unknown" in c["detail"] for c in pre["checks"])

    # Reconciliation reads the target live: the approved value is there.
    r = client.post("/changes/S-EX-TIMEOUT/execution/reconcile", headers=headers("vdb"), json={"note": "checked"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["outcome"], body["observedValue"]) == ("applied", "SO")
    assert body["source"].startswith("live AIS read (")
    assert _state(change["change_id"]) == "applied"
    with pytest.raises(ExecutionBlocked, match="already been applied"):
        _record_applied(change["change_id"])

    ec = client.get("/changes/S-EX-TIMEOUT", headers=headers("vdb")).json()["exactChange"]["execution"]
    assert ec["writeState"] == "applied"
    assert ec["writeReconciliations"][0]["verifiedBy"] == "Hendro"
    assert ec["testReconciliations"] == []


def test_without_a_live_read_a_person_states_the_observed_value_with_evidence(client):
    change = _ready_change(client, "S-EX-HUMAN")
    _legacy_unknown_write(change["change_id"])
    url = "/changes/S-EX-HUMAN/execution/reconcile"
    r = client.post(url, headers=headers("vdb"), json={})
    assert r.status_code == 422 and "Provide observedValue" in r.json()["detail"]
    r = client.post(url, headers=headers("vdb"), json={"observedValue": "SO", "note": "Read in P983051"})
    assert r.status_code == 422 and "evidenceReference" in r.json()["detail"]
    r = client.post(url, headers=headers("vdb"), json={
        "observedValue": "SO", "note": "Read in P983051", "evidenceReference": "screenshot in JIRA-77"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["outcome"], body["observedValue"], body["source"]) == ("applied", "SO", "human-verified in JDE")
    assert _state(change["change_id"]) == "applied"


def test_a_write_that_never_reached_jde_is_reconciled_as_not_applied_and_may_be_recorded(client):
    change = _ready_change(client, "S-EX-LOST")
    _legacy_unknown_write(change["change_id"])
    r = client.post("/changes/S-EX-LOST/execution/reconcile", headers=headers("vdb"),
                    json={"observedValue": "S3", "note": "still S3 in P983051", "evidenceReference": "JIRA-78"})
    assert r.json()["outcome"] == "not_applied"
    assert _state(change["change_id"]) == "ready"

    _execute("S-EX-LOST", change["change_id"])
    assert _state(change["change_id"]) == "applied"
    assert len(_record(change["change_id"])["execution"]["write"]["attempts"]) == 2


def test_a_target_changed_by_someone_else_is_diverged_and_never_runs(client):
    from jde_mcp_server.execution import ExecutionBlocked

    change = _ready_change(client, "S-EX-DIVERGED")
    _legacy_unknown_write(change["change_id"])
    r = client.post("/changes/S-EX-DIVERGED/execution/reconcile", headers=headers("vdb"),
                    json={"observedValue": "SV", "note": "someone set SV", "evidenceReference": "JIRA-79"})
    assert r.json()["outcome"] == "diverged"
    with pytest.raises(ExecutionBlocked, match="neither the before nor the approved"):
        _execute("S-EX-DIVERGED", change["change_id"])


def test_a_write_in_flight_during_a_restart_is_recorded_as_unknown(client):
    from fastapi.testclient import TestClient
    from jde_api_service.main import app
    from jde_mcp_server import execution

    change = _ready_change(client, "S-EX-RESTART")
    # An attempt in flight when the process dies (the persisted state an older
    # automated write, or a live test orchestration, leaves behind).
    execution.begin(change["change_id"], execution.WRITE, before_value="S3")
    with TestClient(app):  # restart
        pass
    assert _state(change["change_id"]) == "unknown"
    assert "Interrupted by a restart" in _record(change["change_id"])["execution"]["write"]["attempts"][-1]["detail"]


def test_an_attempt_left_in_progress_too_long_counts_as_unknown(client):
    from jde_mcp_server import approval, execution

    change = _ready_change(client, "S-EX-STALE")
    execution.begin(change["change_id"], execution.WRITE)
    record = approval._load(change["change_id"])
    record["execution"]["write"]["attempts"][-1]["started_at"] = time.time() - execution.STALE_AFTER_SECONDS - 1
    approval._save(change["change_id"], record)
    assert execution.effective_state(approval._load(change["change_id"])) == "unknown"


def test_reconciling_needs_a_role_the_policy_allows(client):
    change = _ready_change(client, "S-EX-AUTH")
    from jde_mcp_server import execution

    _legacy_unknown_write(change["change_id"])
    _save_scope(client, "vdb", _full_scope(policy={**POLICY, "exactChangeApproverRoles": ["domain_owner"]}))
    # Hendro holds domain_owner too; strip it from the policy check by using a narrower session.
    from fastapi.testclient import TestClient
    from jde_api_service.main import app
    from jde_api_service.services import auth_service, membership_service

    from .conftest import TEST_PASSWORD, _apply_csrf_header

    auth_service.create_user("pm@test.local", TEST_PASSWORD, "Pat Manager", user_id="u-pm")
    membership_service.create_membership("u-pm", "vdb", ["product_manager"], created_by="u-hendro")
    pm = TestClient(app)
    pm.post("/auth/login", json={"email": "pm@test.local", "password": TEST_PASSWORD})
    _apply_csrf_header(pm)
    r = pm.post("/changes/S-EX-AUTH/execution/reconcile", headers=headers("vdb"), json={})
    assert r.status_code == 403


# ---------------------------------------------------------------------
# Test runs
# ---------------------------------------------------------------------
def _change_with_test(client, story_id: str, *, connected: bool = True) -> dict:
    from jde_mcp_server import approval

    if connected:
        from ._discovery import ready_company

        ready_company(client, "vdb")
    _save_scope(client, "vdb", _full_scope())
    _approved_story(story_id)
    change = approval.propose_change(story_id, {**OPERATION, "story_id": story_id, "test_orchestration": "ORCH_SO"},
                                     "processing_option_update")
    _approve(change["change_id"])
    return change


def test_the_test_runs_only_after_the_write_is_applied_and_only_once(client):
    from jde_api_service.delivery import functional
    from jde_mcp_server.execution import ExecutionBlocked

    change = _change_with_test(client, "S-EX-TEST")

    def run():
        return functional.run_test(change["change_id"], actor_user_id="u-hendro", actor_name="Hendro")

    with pytest.raises(ExecutionBlocked, match="not known to be applied"):
        run()
    with pytest.raises(ExecutionBlocked, match="not known to be applied"):
        _record_test(change["change_id"])
    _execute("S-EX-TEST", change["change_id"])
    assert _record_test(change["change_id"])["passed"] is True
    assert _state(change["change_id"], "test") == "completed"
    with pytest.raises(ExecutionBlocked, match="already run"):
        run()
    with pytest.raises(ExecutionBlocked, match="already run"):
        _record_test(change["change_id"])


def test_a_test_orchestration_that_cannot_be_sent_is_not_run_and_stays_ready(client, monkeypatch):
    """The AIS server cannot be reached: nothing was sent, so the attempt is
    'not_sent' (never unknown) and the test may still be run or recorded."""
    import httpx

    from jde_api_service.delivery import functional
    from jde_api_service.discovery import service as discovery_service

    change = _change_with_test(client, "S-EX-NOSEND")
    _execute("S-EX-NOSEND", change["change_id"])

    def down(request):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(discovery_service, "LIVE_HTTP_TRANSPORT", httpx.MockTransport(down))
    with pytest.raises(functional.DeliveryRefused, match="could not be started"):
        functional.run_test(change["change_id"], actor_user_id="u-hendro", actor_name="Hendro")
    assert _state(change["change_id"], "test") == "ready"
    assert _record(change["change_id"])["execution"]["test"]["attempts"][-1]["outcome"] == "not_sent"
    assert _record_test(change["change_id"])["passed"] is True


def test_after_a_stated_apply_the_test_result_can_be_recorded(client):
    """Without a live read (no connection at approval or at recording) the
    person states the applied value with evidence; the next step the
    lifecycle offers -- recording the test result -- must then be possible."""
    change = _change_with_test(client, "S-EX-STATED-TEST", connected=False)
    assert _execute("S-EX-STATED-TEST", change["change_id"])["source"].startswith("stated by Hendro")
    assert _record_test(change["change_id"])["passed"] is True
    assert _state(change["change_id"], "test") == "completed"


def test_an_unknown_test_run_needs_a_human_attestation_with_a_note(client):
    from jde_mcp_server import execution

    change = _ready_change(client, "S-EX-TESTUNK")
    _execute("S-EX-TESTUNK", change["change_id"])
    # A live test orchestration that timed out after sending.
    attempt = execution.begin(change["change_id"], execution.TEST)
    execution.finish(change["change_id"], execution.TEST, attempt, "unknown", "orchestrator timed out")
    url = "/changes/S-EX-TESTUNK/execution/reconcile-test"
    assert client.post(url, headers=headers("vdb"), json={"ran": False, "note": ""}).status_code == 422
    note = "No order was created in DEV (checked P4210 W4210A)."
    assert client.post(url, headers=headers("vdb"), json={"ran": False, "note": note}).status_code == 422  # no evidence
    r = client.post(url, headers=headers("vdb"), json={"ran": False, "note": note, "evidenceReference": "JIRA-123 screenshot"})
    assert r.json()["outcome"] == "not_run"
    assert _state(change["change_id"], "test") == "ready"


# ---------------------------------------------------------------------
# Unsupported operations are refused up front
# ---------------------------------------------------------------------
def test_a_capability_without_an_execution_adapter_cannot_be_proposed_as_executable(client):
    from jde_mcp_server import approval

    _save_scope(client, "vdb", _full_scope())
    _approved_story("S-EX-UNSUPPORTED")
    with pytest.raises(approval.ChangeApprovalError, match="no execution adapter"):
        approval.propose_change("S-EX-UNSUPPORTED", {"tool": "set_processing_option"}, "udc_value_maintenance")
    with pytest.raises(approval.ChangeApprovalError, match="executes only through"):
        approval.propose_change("S-EX-UNSUPPORTED", {"tool": "run_sql"}, "processing_option_update")
