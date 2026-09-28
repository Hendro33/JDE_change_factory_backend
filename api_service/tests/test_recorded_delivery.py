"""
Delivering an approved functional change on the customer's JD Edwards
system -- the recorded route (delivery/functional.py).

A person applies the approved value in DEV and records it; Jade re-checks
the approval, scope and authority and reads the value back LIVE through the
customer's own connection (here: the fake AIS server at the HTTP boundary).
Only the approved value is ever recorded as applied. The approved test
orchestration runs live, or the person records the test result.
"""

from __future__ import annotations

import httpx

from ._discovery import ready_company
from .conftest import headers
from .fixtures.fake_ais import apply_in_dev
from .test_stage1_execution_safeguards import OPERATION, _approve, _approved_story, _full_scope, _save_scope


def _change(client, story: str, *, connected: bool = True, test: str | None = "ORCH_SO") -> dict:
    from jde_mcp_server import approval

    if connected:
        ready_company(client, "vdb")
    _save_scope(client, "vdb", _full_scope())
    _approved_story(story)
    op = {**OPERATION, "story_id": story, **({"test_orchestration": test} if test else {})}
    change = approval.propose_change(story, op, "processing_option_update")
    return _approve(change["change_id"])


def _applied(client, story: str, **body):
    return client.post(f"/changes/{story}/delivery/applied", headers=headers("vdb"),
                       json={"evidenceReference": "", "note": "", **body})


def _next(client, story: str) -> str:
    return client.get(f"/changes/{story}", headers=headers("vdb")).json()["lifecycle"]["nextAction"]["action"]


def test_the_approved_value_is_read_back_live_before_it_is_recorded(client):
    rec = _change(client, "S-RD-LIVE")
    before = rec["binding"]["before_state"]
    assert before["known"] and before["value"] == "S3" and before["source"].startswith("live AIS read"), before
    assert _next(client, "S-RD-LIVE") == "record_applied"
    # Not applied yet: JDE still shows the before value.
    r = _applied(client, "S-RD-LIVE")
    assert r.status_code == 409 and "the value before the change" in r.json()["detail"]
    # Applied wrongly: never recorded.
    apply_in_dev("vdb", "P4210", "CIQ0001", "PDOCTYPE", "SQ")
    r = _applied(client, "S-RD-LIVE")
    assert r.status_code == 409 and "not the approved value" in r.json()["detail"]
    # Applied exactly as approved.
    apply_in_dev("vdb", "P4210", "CIQ0001", "PDOCTYPE", "SO")
    r = _applied(client, "S-RD-LIVE", note="set in P4210 processing options")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["observedValue"] == "SO" and body["beforeValue"] == "S3" and body["source"].startswith("live AIS read")
    # Recorded once only.
    assert _applied(client, "S-RD-LIVE").status_code == 409
    change = client.get("/changes/S-RD-LIVE", headers=headers("vdb")).json()
    ex = change["exactChange"]["execution"]
    assert ex["writeState"] == "applied" and ex["applied"]["observed_value"] == "SO"
    assert change["lifecycle"]["nextAction"]["action"] == "run_or_record_test"


def test_without_a_live_read_the_person_states_the_value_with_evidence(client):
    _change(client, "S-RD-STATED", connected=False)
    r = _applied(client, "S-RD-STATED")
    assert r.status_code == 409 and "State the value you read in JDE" in r.json()["detail"]
    r = _applied(client, "S-RD-STATED", statedValue="SQ", evidenceReference="screenshot 12")
    assert r.status_code == 409 and "not the approved value" in r.json()["detail"]
    r = _applied(client, "S-RD-STATED", statedValue="SO", evidenceReference="screenshot 12")
    assert r.status_code == 200, r.text
    assert r.json()["source"].startswith("stated by Hendro (read in JDE)")


def test_the_approved_test_orchestration_runs_live_on_the_customers_ais(client, ais):
    _change(client, "S-RD-TEST")
    apply_in_dev("vdb", "P4210", "CIQ0001", "PDOCTYPE", "SO")
    r0 = _applied(client, "S-RD-TEST")
    assert r0.status_code == 200, r0.text
    seen = []
    ais.orchestrations["ORCH_SO"] = lambda payload: (seen.append(payload) or (200, {"salesOrder": "10077"}))
    r = client.post("/changes/S-RD-TEST/delivery/run-test", headers=headers("vdb"))
    assert r.status_code == 200, r.text
    assert r.json()["passed"] is True and r.json()["answer"] == {"salesOrder": "10077"}
    assert any(p == "/jderest/v3/orchestrator/ORCH_SO" for _, p, _c in ais.calls)
    assert client.post("/changes/S-RD-TEST/delivery/run-test", headers=headers("vdb")).status_code == 409
    change = client.get("/changes/S-RD-TEST", headers=headers("vdb")).json()
    assert change["exactChange"]["execution"]["verification"]["source"] == "live orchestration"
    assert change["lifecycle"]["nextAction"]["action"] == "finalise_asbuilt"


def test_an_orchestration_that_times_out_is_unknown_until_reconciled(client, ais):
    from .fixtures import ais_estate

    _change(client, "S-RD-TIMEOUT")
    apply_in_dev("vdb", "P4210", "CIQ0001", "PDOCTYPE", "SO")
    r0 = _applied(client, "S-RD-TIMEOUT")
    assert r0.status_code == 200, r0.text
    ais_estate.add_fault("vdb", "JDV920", operation="orchestration", target="ORCH_SO", mode="timeout_after_apply")
    r = client.post("/changes/S-RD-TIMEOUT/delivery/run-test", headers=headers("vdb"))
    assert r.status_code == 409 and "outcome is unknown" in r.json()["detail"]
    assert client.post("/changes/S-RD-TIMEOUT/delivery/run-test", headers=headers("vdb")).status_code == 409
    assert _next(client, "S-RD-TIMEOUT") == "reconcile_functional"
    r = client.post("/changes/S-RD-TIMEOUT/execution/reconcile-test", headers=headers("vdb"),
                    json={"ran": True, "note": "sales order 10078 exists in DEV", "evidenceReference": "F4211 query"})
    assert r.status_code == 200 and r.json()["outcome"] == "completed", r.text


def test_a_person_records_the_test_result_and_a_failure_is_not_released(client):
    _change(client, "S-RD-MANUAL", test=None)
    apply_in_dev("vdb", "P4210", "CIQ0001", "PDOCTYPE", "SO")
    r0 = _applied(client, "S-RD-MANUAL")
    assert r0.status_code == 200, r0.text
    r = client.post("/changes/S-RD-MANUAL/delivery/run-test", headers=headers("vdb"))
    assert r.status_code == 409 and "names no test orchestration" in r.json()["detail"]
    r = client.post("/changes/S-RD-MANUAL/delivery/test-result", headers=headers("vdb"),
                    json={"passed": False, "note": "order type defaulted to SO but pricing broke", "evidenceReference": "T-9"})
    assert r.status_code == 200 and r.json()["passed"] is False, r.text
    change = client.get("/changes/S-RD-MANUAL", headers=headers("vdb")).json()
    assert change["lifecycle"]["health"] == "failed" and change["lifecycle"]["nextAction"]["action"] == "rerun_solutioning"


def test_recording_needs_the_application_manager_role(client, viewer_client):
    _change(client, "S-RD-ROLE", connected=False)
    r = viewer_client.post("/changes/S-RD-ROLE/delivery/applied", headers=headers("vdb"),
                           json={"statedValue": "SO", "evidenceReference": "x"})
    assert r.status_code == 403


def test_after_recording_discovery_reads_the_applied_value(client):
    from jde_api_service.discovery import service as discovery_service

    _change(client, "S-RD-DISC")
    apply_in_dev("vdb", "P4210", "CIQ0001", "PDOCTYPE", "SO")
    r0 = _applied(client, "S-RD-DISC")
    assert r0.status_code == 200, r0.text
    grant, _ = discovery_service.grant_for_story("S-RD-DISC", "vdb", agent_run_id=None, actor_user_id="u-hendro")
    raw: list = []
    discovery_service.execute_read(grant, "processing_option_values", "P4210|CIQ0001", [], None, 10, raw_out=raw)
    assert {r["option"]: r["value"] for r in raw}["PDOCTYPE"] == "SO"


def test_an_unreachable_ais_is_reported_and_nothing_is_recorded(client, monkeypatch):
    from jde_api_service.discovery import service as discovery_service

    _change(client, "S-RD-DOWN")
    apply_in_dev("vdb", "P4210", "CIQ0001", "PDOCTYPE", "SO")

    def down(request):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(discovery_service, "LIVE_HTTP_TRANSPORT", httpx.MockTransport(down))
    r = _applied(client, "S-RD-DOWN")
    assert r.status_code == 409 and "cannot read the value back live" in r.json()["detail"]
    assert client.get("/changes/S-RD-DOWN", headers=headers("vdb")).json()["exactChange"]["execution"]["writeState"] \
        == "ready"
