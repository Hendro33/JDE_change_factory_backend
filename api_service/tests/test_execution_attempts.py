"""
Interrupted and repeated execution (mcp_server/jde_mcp_server/execution.py).

Where a JDE write may have happened, its outcome is recorded as UNKNOWN,
any retry is refused, and only reconciliation against the ACTUAL target
state lets anything run again. A write that is known to be applied never
runs twice under the same approval. Live mode is simulated by replacing
the HTTP call -- no request ever leaves this process.
"""

from __future__ import annotations

import dataclasses
import time

import httpx
import pytest

from .conftest import headers
from .test_stage1_execution_safeguards import (
    POLICY,
    _approve,
    _approved_story,
    _execute,
    _full_scope,
    _propose,
    _save_scope,
)


def _ready_change(client, story_id: str) -> dict:
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


# ---------------------------------------------------------------------
# One approval, one application
# ---------------------------------------------------------------------
def test_an_applied_change_never_runs_twice(client):
    from jde_mcp_server.execution import ExecutionBlocked

    change = _ready_change(client, "S-EX-ONCE")
    result = _execute("S-EX-ONCE", change["change_id"])
    assert result["request"]["previous_value"] == "MOCK-INITIAL"
    assert _state(change["change_id"]) == "applied"
    with pytest.raises(ExecutionBlocked, match="already been applied"):
        _execute("S-EX-ONCE", change["change_id"])


# ---------------------------------------------------------------------
# Unknown outcome: blocked until reconciled against the target
# ---------------------------------------------------------------------
def test_a_write_interrupted_after_jde_applied_it_is_reconciled_as_applied(client, monkeypatch):
    from jde_mcp_server import ais_client
    from jde_mcp_server.execution import ExecutionBlocked

    change = _ready_change(client, "S-EX-TIMEOUT")
    real_submit = ais_client._mock_submit

    def applied_then_timed_out(*args):
        real_submit(*args)  # JDE applied it ...
        raise httpx.ReadTimeout("no response")  # ... but we never heard back

    monkeypatch.setattr(ais_client, "_mock_submit", applied_then_timed_out)
    with pytest.raises(httpx.ReadTimeout):
        _execute("S-EX-TIMEOUT", change["change_id"])
    assert _state(change["change_id"]) == "unknown"

    # No blind retry, even with the original submit restored.
    monkeypatch.setattr(ais_client, "_mock_submit", real_submit)
    with pytest.raises(ExecutionBlocked, match="unknown"):
        _execute("S-EX-TIMEOUT", change["change_id"])

    # The preflight says why, among its checks.
    pre = client.get("/changes/S-EX-TIMEOUT/execution/preflight", headers=headers("vdb")).json()
    assert pre["executable"] is False and pre["writeState"] == "unknown"
    assert any(not c["ok"] and "unknown" in c["detail"] for c in pre["checks"])

    # Reconciliation reads the target: the approved value is there.
    r = client.post("/changes/S-EX-TIMEOUT/execution/reconcile", headers=headers("vdb"), json={"note": "checked"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["outcome"], body["observedValue"], body["source"]) == ("applied", "SO", "automated read (mock JDE)")
    assert _state(change["change_id"]) == "applied"
    with pytest.raises(ExecutionBlocked, match="already been applied"):
        _execute("S-EX-TIMEOUT", change["change_id"])

    ec = client.get("/changes/S-EX-TIMEOUT", headers=headers("vdb")).json()["exactChange"]["execution"]
    assert ec["writeState"] == "applied"
    assert ec["writeReconciliations"][0]["verifiedBy"] == "Hendro"
    assert ec["testReconciliations"] == []


def test_a_write_that_never_reached_jde_is_reconciled_as_not_applied_and_may_run_again(client, monkeypatch):
    from jde_mcp_server import ais_client

    change = _ready_change(client, "S-EX-LOST")
    real_submit = ais_client._mock_submit

    def lost(*args):
        raise httpx.ReadTimeout("dropped before JDE saw it")

    monkeypatch.setattr(ais_client, "_mock_submit", lost)
    with pytest.raises(httpx.ReadTimeout):
        _execute("S-EX-LOST", change["change_id"])
    assert _state(change["change_id"]) == "unknown"

    r = client.post("/changes/S-EX-LOST/execution/reconcile", headers=headers("vdb"), json={})
    assert r.json()["outcome"] == "not_applied"
    assert _state(change["change_id"]) == "ready"

    monkeypatch.setattr(ais_client, "_mock_submit", real_submit)
    _execute("S-EX-LOST", change["change_id"])
    assert _state(change["change_id"]) == "applied"
    assert len(_record(change["change_id"])["execution"]["write"]["attempts"]) == 2


def test_a_target_changed_by_someone_else_is_diverged_and_never_runs(client, monkeypatch):
    from jde_mcp_server import ais_client
    from jde_mcp_server.execution import ExecutionBlocked

    change = _ready_change(client, "S-EX-DIVERGED")

    def someone_else_changed_it(application, version, option, value):
        ais_client._write_mock_state({ais_client._mock_key(application, version, option): "SV"})
        raise httpx.ReadTimeout("no response")

    monkeypatch.setattr(ais_client, "_mock_submit", someone_else_changed_it)
    with pytest.raises(httpx.ReadTimeout):
        _execute("S-EX-DIVERGED", change["change_id"])
    r = client.post("/changes/S-EX-DIVERGED/execution/reconcile", headers=headers("vdb"), json={})
    assert r.json()["outcome"] == "diverged"
    with pytest.raises(ExecutionBlocked, match="neither the before nor the approved"):
        _execute("S-EX-DIVERGED", change["change_id"])


def test_a_write_in_flight_during_a_restart_is_recorded_as_unknown(client):
    from fastapi.testclient import TestClient
    from jde_api_service.main import app
    from jde_mcp_server import execution

    change = _ready_change(client, "S-EX-RESTART")
    execution.begin(change["change_id"], execution.WRITE, before_value="MOCK-INITIAL")  # then the process dies
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

    execution.begin(change["change_id"], execution.WRITE)
    execution.finish(change["change_id"], execution.WRITE, _record(change["change_id"])["execution"]["write"]["attempts"][-1]["attempt_id"], "unknown")
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
def test_the_test_runs_only_after_the_write_is_applied_and_only_once(client):
    from jde_mcp_server.ais_client import client as ais
    from jde_mcp_server.execution import ExecutionBlocked

    _save_scope(client, "vdb", _full_scope())
    _approved_story("S-EX-TEST")
    from jde_mcp_server import approval

    change = approval.propose_change(
        "S-EX-TEST",
        {"tool": "set_processing_option", "story_id": "S-EX-TEST", "application": "P4210", "version": "CIQ0001",
         "option": "PDOCTYPE", "value": "SO", "test_orchestration": "ORCH_SO"},
        "processing_option_update",
    )
    _approve(change["change_id"])
    with pytest.raises(ExecutionBlocked, match="not known to be applied"):
        ais.run_orchestration("S-EX-TEST", change["change_id"], "ORCH_SO", {})
    ais.set_processing_option("S-EX-TEST", change["change_id"], "P4210", "CIQ0001", "PDOCTYPE", "SO")
    assert ais.run_orchestration("S-EX-TEST", change["change_id"], "ORCH_SO", {})["request"]["result"] == "PASS"
    with pytest.raises(ExecutionBlocked, match="already run"):
        ais.run_orchestration("S-EX-TEST", change["change_id"], "ORCH_SO", {})


def test_an_unknown_test_run_needs_a_human_attestation_with_a_note(client):
    from jde_mcp_server import execution

    change = _ready_change(client, "S-EX-TESTUNK")
    _execute("S-EX-TESTUNK", change["change_id"])
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


# ---------------------------------------------------------------------
# Live mode (simulated: the HTTP call is replaced, nothing leaves the process)
# ---------------------------------------------------------------------
def _live(monkeypatch, environment: str = "JDV920"):
    from jde_mcp_server import ais_client

    monkeypatch.setattr(
        ais_client, "settings",
        dataclasses.replace(ais_client.settings, mock_mode=False, ais_environment=environment),
    )
    monkeypatch.setattr(ais_client, "FSR_SET_PROCESSING_OPTION", {"value": "{{VALUE}}"})
    monkeypatch.setattr(ais_client.client, "_headers", lambda: {"AIS-Auth-Token": "test"})
    sent = []

    def fake_post(url, headers=None, json=None):
        sent.append(url)
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

    monkeypatch.setattr(ais_client.client._http, "post", fake_post)
    return sent


def test_live_mode_refuses_when_the_connection_is_not_the_bound_environment(client, monkeypatch):
    from jde_mcp_server.ais_client import AISClientError

    change = _ready_change(client, "S-EX-WRONGENV")
    sent = _live(monkeypatch, environment="JPY920")
    with pytest.raises(AISClientError, match="bound to 'JDV920'"):
        _execute("S-EX-WRONGENV", change["change_id"])
    assert sent == []
    assert _state(change["change_id"]) == "ready"  # nothing was attempted


def test_a_live_write_is_unknown_until_a_person_verifies_the_target(client, monkeypatch):
    from jde_mcp_server.execution import ExecutionBlocked

    change = _ready_change(client, "S-EX-LIVE")
    sent = _live(monkeypatch)
    _execute("S-EX-LIVE", change["change_id"])
    assert len(sent) == 1
    # An HTTP 200 is not yet accepted as proof (Experiment A validates the response shape).
    assert _state(change["change_id"]) == "unknown"
    with pytest.raises(ExecutionBlocked):
        _execute("S-EX-LIVE", change["change_id"])

    url = "/changes/S-EX-LIVE/execution/reconcile"
    r = client.post(url, headers=headers("vdb"), json={})
    assert r.status_code == 422 and "Read the value in JDE" in r.json()["detail"]
    r = client.post(url, headers=headers("vdb"), json={"observedValue": "SO", "note": "Read in P983051 after the write"})
    assert r.status_code == 422 and "evidenceReference" in r.json()["detail"]
    r = client.post(url, headers=headers("vdb"), json={
        "observedValue": "SO", "note": "Read in P983051 after the write", "evidenceReference": "screenshot in JIRA-77",
    })
    body = r.json()
    assert (body["outcome"], body["observedValue"], body["source"]) == ("applied", "SO", "human-verified in JDE")
