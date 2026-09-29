"""
The agents make the approved changes in DEV (api_service executors/).

  * Administration: the DEV write user, the web client and Web OMW
    addresses and the on/off switches are per customer, saved, tested and
    audited; the password is never returned.
  * The AIS executor applies exactly each approved item through the JD
    Edwards configuration applications, signed in as the write user, with
    a live read before and after, and never presses a button it cannot
    confirm (never Delete).
  * An item whose outcome is not established stops the change set and is
    reconciled -- never retried blindly. A person records only the items
    marked for a person, or an agent item no agent can apply now.

The fake AIS server (tests/fixtures/fake_ais.py) stands in for the
customer's AIS at the HTTP boundary -- a test boundary only.
"""

from __future__ import annotations

import pytest

from ._discovery import ready_company, sim_edit
from .conftest import headers
from .fixtures.fake_ais import apply_row_in_dev
from .test_configuration_change_sets import NEW_ORDER_TYPE, READS, SCOPE
from .test_stage1_execution_safeguards import _approve, _approved_story, _save_scope

H = headers("vdb")
WRITE_PASSWORD = "Wr1te-user-secret-pw"
AIS_ITEMS = {"tool": "configuration_change_set", "summary": "Webshop order type SW",
             "items": NEW_ORDER_TYPE["items"][:2]}


def fake_ais():
    from jde_api_service.discovery import service

    return service.LIVE_HTTP_TRANSPORT.handler.__self__


def setup_execution(client, *, web_client_url: str = "", test: bool = True) -> dict:
    current = client.get("/admin/jde/execution", headers=H).json()
    r = client.put("/admin/jde/execution", headers=H, json={
        "webClientUrl": web_client_url, "webOmwUrl": "", "writeRole": "JADEWRITE", "notes": "DEV only",
        "expectedRevision": current["revision"] if current["configured"] else None})
    assert r.status_code == 200, r.text
    r = client.put("/admin/jde/execution/write-user", headers=H, json={
        "username": "JADEWRITE", "password": WRITE_PASSWORD, "expectedRevision": r.json()["revision"]})
    assert r.status_code == 200, r.text
    if test:
        r = client.post("/admin/jde/execution/test", headers=H)
        assert r.status_code == 200, r.text
        assert r.json()["results"]["ais_write_sign_in"]["state"] == "ok", r.json()
    return client.get("/admin/jde/execution", headers=H).json()


@pytest.fixture()
def ready(client):
    ready_company(client, "vdb", approvedReads=READS)
    _save_scope(client, "vdb", SCOPE)
    return client


def _propose(story: str, operation: dict) -> dict:
    from jde_mcp_server import approval

    _approved_story(story)
    return approval.propose_change(story, {**operation}, "configuration_change_set")


def _items(client, story: str) -> dict:
    return {i["id"]: i for i in client.get(f"/changes/{story}", headers=H).json()["exactChange"]["items"]}


# ---------------------------------------------------------------------
# Administration
# ---------------------------------------------------------------------
def test_the_write_user_and_switches_are_per_customer_saved_tested_and_audited(ready):
    client = ready
    view = setup_execution(client)
    assert view["writeUserConfigured"] and view["writeUserMasked"].startswith("JA") and view["writeUserStorage"] == "encrypted"
    assert WRITE_PASSWORD not in str(view)
    # The test signed in as the WRITE user, with the write role, in DEV -- not the discovery user.
    assert ("vdb", "JADEWRITE", "JADEWRITE") in fake_ais().signed_in
    assert view["checks"]["ais_write_sign_in"]["state"] == "ok"
    assert view["routes"]["ais"]["ready"], view["routes"]
    assert view["connection"]["environment"] == "JDV920" and view["connection"]["discoveryEnabled"]
    # Switching agent execution off for a capability is recorded with name and date, and holds the route.
    r = client.put("/admin/jde/execution/switch", headers=H, json={"capabilityId": "udc_value_maintenance",
                                                                   "enabled": False, "reason": "month-end freeze"})
    assert r.status_code == 200, r.text
    cap = {c["capabilityId"]: c for c in r.json()["capabilities"]}["udc_value_maintenance"]
    assert not cap["enabled"] and "switched off by Hendro" in cap["detail"]
    assert any(a["action"] == "switch" and "month-end freeze" in a["detail"] and a["actor"] == "Hendro"
               for a in r.json()["audit"])
    from jde_api_service.executors import routes

    assert routes.readiness("vdb", "ais", "udc_value_maintenance")[0] is False
    assert routes.readiness("vdb", "ais", "document_type_definition")[0] is True
    # The customer-wide switch.
    client.put("/admin/jde/execution/switch", headers=H, json={"enabled": False})
    assert routes.readiness("vdb", "ais", "document_type_definition") == (
        False, "agent execution is switched off for this customer by an Admin")
    # A material change (another role) makes the last test stale: agents stop until it is tested again.
    client.put("/admin/jde/execution/switch", headers=H, json={"enabled": True})
    cur = client.get("/admin/jde/execution", headers=H).json()
    client.put("/admin/jde/execution", headers=H, json={**cur["config"], "writeRole": "JADEWRITE2",
                                                        "expectedRevision": cur["revision"]})
    view = client.get("/admin/jde/execution", headers=H).json()
    assert view["checks"]["ais_write_sign_in"]["state"] == "stale" and not view["routes"]["ais"]["ready"]


def test_the_write_user_must_differ_from_the_discovery_user_and_only_admins_change_settings(ready):
    client = ready
    r = client.put("/admin/jde/execution", headers=H, json={"writeRole": "JADEWRITE"})
    assert r.status_code == 200
    r = client.put("/admin/jde/execution/write-user", headers=H, json={"username": "jadedisc", "password": "x",
                                                                       "expectedRevision": r.json()["revision"]})
    assert r.status_code == 422 and "different user" in r.json()["detail"]
    r = client.put("/admin/jde/execution", headers=H, json={"writeRole": "*ALL", "expectedRevision": 1})
    assert r.status_code == 422


# ---------------------------------------------------------------------
# The AIS executor
# ---------------------------------------------------------------------
def test_the_agents_apply_each_approved_item_through_ais_and_read_it_back(ready):
    client = ready
    setup_execution(client)
    rec = _propose("S-AG-1", AIS_ITEMS)
    items = rec["operation"]["items"]
    assert [(i["executor"], i["route"]) for i in items] == [("agent", "ais"), ("agent", "ais")]
    assert "P0004A" in items[0]["route_reason"] and "P40040" in items[1]["route_reason"]
    _approve(rec["change_id"])
    fake = fake_ais()
    fake.form_actions.clear()
    r = client.post("/changes/S-AG-1/delivery/agents/run", headers=H)
    assert r.status_code == 202, r.text
    view = _items(client, "S-AG-1")
    assert view["I1"]["deliveryState"] == "applied" and view["I2"]["deliveryState"] == "applied", view
    for i in ("I1", "I2"):
        applied = view[i]["applied"]
        assert applied["executor"] == "agent" and applied["route"] == "ais" and applied["live"]
        assert applied["source"].startswith("live AIS read") and applied["before"] == {"exists": False, "values": {}}
    assert view["I1"]["applied"]["observed"] == {"exists": True, "values": {"DRDL01": "Sales Order - Webshop"}}
    # Exactly the approved values are in DEV.
    from .fixtures import ais_estate

    tables = ais_estate.load("vdb", "JDV920")["tables"]
    assert {"DRSY": "00", "DRRT": "DT", "DRKY": "SW", "DRDL01": "Sales Order - Webshop"}.items() <= next(
        r for r in tables["F0005"] if r.get("DRKY") == "SW").items()
    assert next(r for r in tables["F40039"] if r.get("DCTO") == "SW") == {
        "DCTO": "SW", "DCT4": "SO", "DCDL01": "Sales Order - Webshop"}
    # Forms opened read-only; only the confirmed Find/Add/OK buttons pressed; the write user signed in.
    pressed = [a["controlID"] for a in fake.form_actions if a.get("command") == "DoAction"]
    assert pressed == ["15", "16", "11", "15", "16", "11"]
    assert ("vdb", "JADEWRITE", "JADEWRITE") in fake.signed_in
    change = client.get("/changes/S-AG-1", headers=H).json()
    assert change["exactChange"]["execution"]["writeState"] == "applied"
    assert change["lifecycle"]["nextAction"]["action"] in ("record_test_result", "run_or_record_test")
    # The evidence chain records both agent applications.
    from jde_mcp_server.evidence import verify_chain

    assert verify_chain("S-AG-1")["valid"]


def test_an_update_selects_exactly_the_row_and_changes_only_the_approved_fields(ready):
    client = ready
    setup_execution(client)
    apply_row_in_dev("vdb", "F0005", {"DRSY": "00", "DRRT": "DT", "DRKY": "SW"},
                     {"DRDL01": "Old text", "DRSPHD": "", "DRHRDC": "N"})
    rec = _propose("S-AG-UPD", {"tool": "configuration_change_set", "items": [
        {"capability_id": "udc_value_maintenance", "product_code": "00", "udc_type": "DT", "code": "SW",
         "action": "update", "values": {"DRDL01": "Webshop order"}}]})
    _approve(rec["change_id"])
    client.post("/changes/S-AG-UPD/delivery/agents/run", headers=H)
    assert _items(client, "S-AG-UPD")["I1"]["deliveryState"] == "applied"
    from .fixtures import ais_estate

    row = next(r for r in ais_estate.load("vdb", "JDV920")["tables"]["F0005"] if r.get("DRKY") == "SW")
    assert row["DRDL01"] == "Webshop order" and row["DRHRDC"] == "N"


def test_a_person_records_only_their_own_items(ready):
    client = ready
    setup_execution(client)
    rec = _propose("S-AG-PERSON", AIS_ITEMS)
    _approve(rec["change_id"])
    apply_row_in_dev("vdb", "F0005", {"DRSY": "00", "DRRT": "DT", "DRKY": "SW"}, {"DRDL01": "Sales Order - Webshop"})
    r = client.post("/changes/S-AG-PERSON/delivery/applied", headers=H,
                    json={"itemId": "I1", "evidenceReference": "", "note": ""})
    assert r.status_code == 409 and "applied by the agents" in r.json()["detail"]


def test_an_agent_item_is_handed_to_a_person_only_when_no_agent_can_apply_it(ready):
    client = ready
    setup_execution(client)
    client.put("/admin/jde/execution/switch", headers=H, json={"capabilityId": "udc_value_maintenance",
                                                               "enabled": False})
    rec = _propose("S-AG-HAND", AIS_ITEMS)
    _approve(rec["change_id"])
    r = client.post("/changes/S-AG-HAND/delivery/agents/run", headers=H)
    assert r.status_code == 202
    view = _items(client, "S-AG-HAND")
    assert view["I1"]["deliveryState"] == "agent_unavailable" and view["I1"]["handoverAllowed"]
    assert "switched off by Hendro" in view["I1"]["deliveryDetail"]
    assert view["I2"]["deliveryState"] == "waiting_for_agent"  # the run stops at I1: order matters
    nxt = client.get("/changes/S-AG-HAND", headers=H).json()["lifecycle"]["nextAction"]
    assert nxt["action"] == "run_agents_or_record" and "I1" in nxt["summary"]
    # The person applies I1 and records it: the hand-over is recorded, then the agents continue with I2.
    apply_row_in_dev("vdb", "F0005", {"DRSY": "00", "DRRT": "DT", "DRKY": "SW"}, {"DRDL01": "Sales Order - Webshop"})
    r = client.post("/changes/S-AG-HAND/delivery/applied", headers=H,
                    json={"itemId": "I1", "evidenceReference": "", "note": ""})
    assert r.status_code == 200, r.text
    assert "handed to a person" in r.json()["handover"]
    view = _items(client, "S-AG-HAND")
    assert view["I1"]["applied"]["executor"] == "person" and view["I2"]["deliveryState"] == "applied"
    assert view["I2"]["applied"]["executor"] == "agent"


def test_a_button_ais_does_not_report_is_never_pressed(ready):
    client = ready
    setup_execution(client)
    fake = fake_ais()
    fake.report_action_controls = False
    rec = _propose("S-AG-NOBTN", AIS_ITEMS)
    _approve(rec["change_id"])
    fake.form_actions.clear()
    client.post("/changes/S-AG-NOBTN/delivery/agents/run", headers=H)
    view = _items(client, "S-AG-NOBTN")
    assert view["I1"]["deliveryState"] == "agent_could_not_apply" and view["I1"]["handoverAllowed"]
    assert "did not report the form's buttons" in view["I1"]["attempts"][-1]["detail"]
    assert not any(a.get("command") == "DoAction" for a in fake.form_actions)
    from .fixtures import ais_estate

    assert not any(r.get("DRKY") == "SW" for r in ais_estate.load("vdb", "JDV920")["tables"].get("F0005", []))
    # Nothing was sent, so once the cause is fixed a person may ask the agents to try again.
    fake.report_action_controls = True
    client.post("/changes/S-AG-NOBTN/delivery/agents/run", headers=H)
    view = _items(client, "S-AG-NOBTN")
    assert view["I1"]["deliveryState"] == "applied" and len(view["I1"]["attempts"]) == 2
    assert view["I2"]["deliveryState"] == "applied"


def test_a_commit_that_fails_stops_the_item_for_reconciliation_never_a_blind_retry(ready):
    from .fixtures import ais_estate

    client = ready
    setup_execution(client)
    rec = _propose("S-AG-FAIL", AIS_ITEMS)
    _approve(rec["change_id"])
    ais_estate.add_fault("vdb", "JDV920", operation="form_commit", target="P0004A_W0004AB", mode="fail",
                         message="JDE rejected the commit")
    client.post("/changes/S-AG-FAIL/delivery/agents/run", headers=H)
    view = _items(client, "S-AG-FAIL")
    assert view["I1"]["deliveryState"] == "unknown", view["I1"]
    assert "reconciliation" in view["I1"]["deliveryDetail"]
    nxt = client.get("/changes/S-AG-FAIL", headers=H).json()["lifecycle"]["nextAction"]
    assert nxt["action"] == "reconcile_item" and "I1" in nxt["summary"]
    # Running again does not retry the item.
    client.post("/changes/S-AG-FAIL/delivery/agents/run", headers=H)
    assert len(_items(client, "S-AG-FAIL")["I1"]["attempts"]) == 1
    # A person cannot record it over the unknown outcome either.
    r = client.post("/changes/S-AG-FAIL/delivery/applied", headers=H, json={"itemId": "I1", "evidenceReference": "x"})
    assert r.status_code == 409 and "reconcile" in r.json()["detail"]
    # Reconciled by a live read: still the state before the change -> ready again, and the agents can run it.
    r = client.post("/changes/S-AG-FAIL/delivery/items/I1/reconcile", headers=H, json={"note": "checked"})
    assert r.status_code == 200 and r.json()["outcome"] == "not_applied", r.text
    client.post("/changes/S-AG-FAIL/delivery/agents/run", headers=H)
    view = _items(client, "S-AG-FAIL")
    assert view["I1"]["deliveryState"] == "applied" and view["I2"]["deliveryState"] == "applied"


def test_a_commit_whose_answer_is_lost_is_settled_by_the_live_read_back(ready):
    from .fixtures import ais_estate

    client = ready
    setup_execution(client)
    rec = _propose("S-AG-TIMEOUT", AIS_ITEMS)
    _approve(rec["change_id"])
    ais_estate.add_fault("vdb", "JDV920", operation="form_commit", target="P0004A_W0004AB",
                         mode="commit_then_timeout")
    client.post("/changes/S-AG-TIMEOUT/delivery/agents/run", headers=H)
    view = _items(client, "S-AG-TIMEOUT")
    assert view["I1"]["deliveryState"] == "applied" and "did not answer cleanly" in view["I1"]["attempts"][-1]["detail"]


def test_dev_changed_since_approval_stops_the_item_before_anything_is_sent(ready):
    client = ready
    setup_execution(client)
    rec = _propose("S-AG-DRIFT", AIS_ITEMS)
    _approve(rec["change_id"])
    apply_row_in_dev("vdb", "F0005", {"DRSY": "00", "DRRT": "DT", "DRKY": "SW"}, {"DRDL01": "someone else"})
    fake = fake_ais()
    fake.form_actions.clear()
    client.post("/changes/S-AG-DRIFT/delivery/agents/run", headers=H)
    view = _items(client, "S-AG-DRIFT")
    assert view["I1"]["deliveryState"] == "agent_could_not_apply"
    assert "no longer shows the state the approval was bound to" in view["I1"]["attempts"][-1]["detail"]
    assert fake.form_actions == []


def test_the_scope_is_rechecked_for_every_item_and_writes_can_be_paused(ready, tmp_path, monkeypatch):
    client = ready
    setup_execution(client)
    rec = _propose("S-AG-SCOPE", AIS_ITEMS)
    _approve(rec["change_id"])
    # Writes paused (backup/restore): nothing starts.
    import os

    pause = os.environ["JDE_WRITE_PAUSE_FILE"]
    open(pause, "w").close()
    from jde_api_service.executors import runner
    from jde_mcp_server import execution

    with pytest.raises(execution.ExecutionBlocked, match="paused"):
        runner.run_item(rec["change_id"], rec["operation"]["items"][0], initiator_user_id="u-hendro",
                        initiator_name="Hendro")
    os.remove(pause)
    # The customer's scope no longer lists 00/DT: the run is refused before anything is sent.
    scope = {**SCOPE, "functionalAgent": {**SCOPE["functionalAgent"], "approvedConfiguration": [
        e for e in SCOPE["functionalAgent"]["approvedConfiguration"] if e["target"] != "00/DT"]}}
    _save_scope(client, "vdb", scope)
    r = client.post("/changes/S-AG-SCOPE/delivery/agents/run", headers=H)
    assert r.status_code == 409 and ("00/DT" in r.json()["detail"] or "scope" in r.json()["detail"]), r.text


def test_an_attempt_interrupted_by_a_restart_is_unknown(ready):
    client = ready
    setup_execution(client)
    rec = _propose("S-AG-RESTART", AIS_ITEMS)
    _approve(rec["change_id"])
    from jde_mcp_server import approval, execution

    execution.begin_item(rec["change_id"], "I1", route="ais", agent="Functional Agent (AIS)")
    assert execution.mark_interrupted_unknown() >= 1
    assert execution.item_state(approval._load(rec["change_id"]), "I1") == "unknown"


def test_a_table_no_form_covers_goes_to_a_person(ready):
    from jde_mcp_server import config_items

    item = {"kind": "setup_row", "table": "F4009", "key": {"DMCO": "00000"}, "values": {"DMX": "Y"}, "action": "update"}
    assert config_items.route(item)[0] == "person"
    assert config_items.route({"kind": "version_data_selection"})[0] == "browser"
    assert config_items.route({"kind": "processing_option"})[0] == "browser"


def test_a_form_that_cannot_be_opened_stops_the_item_before_anything_is_saved(ready, monkeypatch):
    from jde_api_service.executors import ais

    client = ready
    setup_execution(client)
    monkeypatch.setitem(ais.FORM_MAPS, "F0005", {**ais.FORM_MAPS["F0005"], "work_with": "W0004ZZ"})
    rec = _propose("S-AG-NOFORM", AIS_ITEMS)
    _approve(rec["change_id"])
    client.post("/changes/S-AG-NOFORM/delivery/agents/run", headers=H)
    view = _items(client, "S-AG-NOFORM")
    assert view["I1"]["deliveryState"] == "agent_could_not_apply", view["I1"]
    assert "before anything was saved" in view["I1"]["attempts"][-1]["detail"]
