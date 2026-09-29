"""
Configuration change sets (jde_mcp_server/config_items.py): the Functional
Agent's real job -- configuring JD Edwards to the customer's standards --
proposed as an ordered set of items under the eight configuration
capabilities, each checked against the universal rules and the customer's
approved configuration when proposed, approved and delivered; applied in DEV
by a person item by item and read back live (the fake AIS server stands in
for the customer's AIS at the HTTP boundary).
"""

from __future__ import annotations

import pytest

from ._discovery import profile_body, ready_company
from .conftest import headers
from .fixtures.fake_ais import apply_in_dev, apply_row_in_dev
from .test_stage1_execution_safeguards import POLICY, _approve, _approved_story, _save_scope

READS = [
    {"capabilityId": "udc_values", "targets": ["00/DT"], "fields": ["DRSY", "DRRT", "DRKY", "DRDL01", "DRSPHD"],
     "filterFields": ["DRKY"]},
    {"capabilityId": "processing_option_values", "targets": ["P4210|CIQ0001"]},
    {"capabilityId": "table_browse", "targets": ["F00941", "F40039"],
     "fields": ["EMENHV", "EMPATHCD", "DCTO", "DCT4", "DCDL01"], "filterFields": ["EMENHV", "DCTO"]},
]

SCOPE = {
    "toolsRelease": "9.2.8.2",
    "environment": {"devEnvironmentId": "JDV920", "devPathCode": "DV920", "aisDataSourceName": "Business Data - DEV",
                    "isolationConfirmed": True, "isolationEvidence": "OCM mappings reviewed"},
    "functionalAgent": {
        "approvedVersions": [{"capabilityId": "processing_option_update", "optionCategory": "document_and_order_types",
                              "application": "P4210", "version": "CIQ0001", "options": ["PDOCTYPE"],
                              "allowedValues": ["SO", "SW"]}],
        "approvedConfiguration": [
            {"capabilityId": "udc_value_maintenance", "category": "document_and_order_types", "target": "00/DT",
             "fields": ["DRDL01", "DRSPHD"], "actions": ["add", "update"]},
            {"capabilityId": "document_type_definition", "category": "document_and_order_types",
             "target": "F40039:DCTO=SW|SX", "fields": ["DCT4", "DCDL01"], "actions": ["add"],
             "allowedValues": {"DCT4": ["SO"]}},
            {"capabilityId": "batch_version_data_selection", "category": "workflow_and_status",
             "target": "R42565|CIQ0001", "actions": []},
        ],
    },
    "approvalPolicy": POLICY,
    "mechanismsAllowed": ["ais_form_service_request", "ais_orchestration"],
    "testScope": {"approvedTests": [{"orchestration": "ORCH_SO", "sideEffects": ["creates_dev_transaction"]}]},
}

NEW_ORDER_TYPE = {
    "tool": "configuration_change_set",
    "summary": "Webshop orders get their own order type SW, set up like SO",
    "test_orchestration": "ORCH_SO",
    "items": [
        {"capability_id": "udc_value_maintenance", "product_code": "00", "udc_type": "DT", "code": "SW",
         "action": "add", "values": {"DRDL01": "Sales Order - Webshop"}, "purpose": "the new order type code"},
        {"capability_id": "document_type_definition", "table": "F40039", "key": {"DCTO": "SW"}, "action": "add",
         "values": {"DCT4": "SO", "DCDL01": "Sales Order - Webshop"}, "purpose": "document type master, category SO"},
        {"capability_id": "processing_option_update", "application": "P4210", "version": "CIQ0001",
         "option": "PDOCTYPE", "value": "SW", "purpose": "webshop entry defaults to SW"},
        {"capability_id": "batch_version_data_selection", "application": "R42565", "version": "CIQ0001",
         "specification": "Order Type (DCTO) is equal to SW", "purpose": "invoice print picks up webshop orders"},
    ],
}


@pytest.fixture()
def ready(client):
    ready_company(client, "vdb", approvedReads=READS)
    _save_scope(client, "vdb", SCOPE)
    return client


def _propose(story: str, operation: dict | None = None) -> dict:
    from jde_mcp_server import approval

    _approved_story(story)
    return approval.propose_change(story, {**(operation or NEW_ORDER_TYPE)}, "configuration_change_set")


def _record(client, story: str, **body):
    return client.post(f"/changes/{story}/delivery/applied", headers=headers("vdb"),
                       json={"evidenceReference": "", "note": "", **body})


def test_a_new_order_type_is_proposed_approved_applied_and_verified_item_by_item(ready):
    client = ready
    rec = _propose("S-CS-1")
    assert rec["capability_id"] == "configuration_change_set" and [i["id"] for i in rec["operation"]["items"]] == [
        "I1", "I2", "I3", "I4"]
    assert all(i["capability_revision"] == "r2" for i in rec["operation"]["items"])
    approved = _approve(rec["change_id"])
    items = approved["binding"]["before_state"]["items"]
    # Read live at approval: the new UDC and document type do not exist yet; the option is S3;
    # a batch version's data selection cannot be read through AIS.
    assert items["I1"]["value"] == {"exists": False, "values": {}}
    assert items["I2"]["value"] == {"exists": False, "values": {}}
    assert items["I3"]["value"] == "S3"
    assert not items["I4"]["known"] and "does not expose" in items["I4"]["reason"]
    ec = client.get("/changes/S-CS-1", headers=headers("vdb")).json()["exactChange"]
    assert [i["id"] for i in ec["items"]] == ["I1", "I2", "I3", "I4"] and ec["items"][2]["before"] == "S3"

    # Not yet applied in DEV: nothing is recorded.
    r = _record(client, "S-CS-1", itemId="I1")
    assert r.status_code == 409 and "still shows the state before" in r.json()["detail"]
    # Applied with a different description: refused, never recorded.
    apply_row_in_dev("vdb", "F0005", {"DRSY": "00", "DRRT": "DT", "DRKY": "SW"}, {"DRDL01": "Webshop", "DRSPHD": ""})
    r = _record(client, "S-CS-1", itemId="I1")
    assert r.status_code == 409 and "not show exactly the approved values" in r.json()["detail"]
    apply_row_in_dev("vdb", "F0005", {"DRSY": "00", "DRRT": "DT", "DRKY": "SW"}, {"DRDL01": "Sales Order - Webshop"})
    r = _record(client, "S-CS-1", itemId="I1")
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "item_applied" and r.json()["remaining"] == 3 and r.json()["source"].startswith(
        "live AIS read")
    assert _record(client, "S-CS-1", itemId="I1").status_code == 409  # recorded once only
    apply_row_in_dev("vdb", "F40039", {"DCTO": "SW"}, {"DCT4": "SO", "DCDL01": "Sales Order - Webshop"})
    assert _record(client, "S-CS-1", itemId="I2").status_code == 200
    apply_in_dev("vdb", "P4210", "CIQ0001", "PDOCTYPE", "SW")
    assert _record(client, "S-CS-1").json()["itemId"] == "I3"  # the next item when none is named
    # The data selection cannot be read: the person confirms it with evidence.
    r = _record(client, "S-CS-1", itemId="I4")
    assert r.status_code == 409 and "evidence reference" in r.json()["detail"]
    r = _record(client, "S-CS-1", itemId="I4", evidenceReference="screenshot R42565 CIQ0001 data selection")
    assert r.status_code == 409 and "confirm" in r.json()["detail"]
    r = _record(client, "S-CS-1", itemId="I4", evidenceReference="screenshot R42565 CIQ0001 data selection",
                confirmedAsSpecified=True)
    assert r.status_code == 200 and r.json()["outcome"] == "applied", r.text
    change = client.get("/changes/S-CS-1", headers=headers("vdb")).json()
    assert change["exactChange"]["execution"]["writeState"] == "applied"
    assert all(i["applied"] for i in change["exactChange"]["items"])
    assert change["lifecycle"]["nextAction"]["action"] == "run_or_record_test"
    # The as-built record lists every item: before, approved, read back (live, or stated with evidence).
    from jde_api_service.process import asbuilt

    rec = client.post("/changes/S-CS-1/as-built", headers=headers("vdb")).json()
    items = rec["content"]["implementation"]["functional"]["items"]
    assert [i["id"] for i in items] == ["I1", "I2", "I3", "I4"]
    assert all(i["readback"]["matches_approved"] for i in items)
    assert [i["readback"]["live"] for i in items] == [True, True, True, False]
    assert any("I4" in x and "could not be read back live" in x for x in rec["content"]["limitations"])
    md = asbuilt.markdown(rec)
    assert "| I2 | Add F40039" in md and "every item matches" in md


def test_the_next_action_names_the_next_item_and_who_applies_it(ready):
    """Every item here has an agent route, but agent execution is not set up
    for this customer: the next action says so, item by item, and a person
    may apply it instead (the hand-over is recorded)."""
    client = ready
    rec = _propose("S-CS-NEXT")
    assert [(i["executor"], i["route"]) for i in rec["operation"]["items"]] == [
        ("agent", "ais"), ("agent", "ais"), ("agent", "browser"), ("agent", "browser")]
    _approve(rec["change_id"])
    nxt = client.get("/changes/S-CS-NEXT", headers=headers("vdb")).json()["lifecycle"]["nextAction"]
    assert nxt["action"] == "run_agents_or_record" and "I1" in nxt["summary"] and "not set up" in nxt["summary"]
    apply_row_in_dev("vdb", "F0005", {"DRSY": "00", "DRRT": "DT", "DRKY": "SW"}, {"DRDL01": "Sales Order - Webshop"})
    r = _record(client, "S-CS-NEXT", itemId="I1")
    assert r.status_code == 200 and "handed to a person" in r.json()["handover"]
    nxt = client.get("/changes/S-CS-NEXT", headers=headers("vdb")).json()["lifecycle"]["nextAction"]
    assert "I2" in nxt["summary"]


@pytest.mark.parametrize("change, reason", [
    ({"capability_id": "udc_value_maintenance", "product_code": "00", "udc_type": "DT", "code": "SW", "action": "add",
      "values": {"DRDL02": "x"}}, "may not be changed"),
    ({"capability_id": "document_type_definition", "table": "F40039", "key": {"DCTO": "SW"}, "action": "update",
      "values": {"DCT4": "SO"}}, "allows add"),
    ({"capability_id": "document_type_definition", "table": "F40039", "key": {"DCTO": "SZ"}, "action": "add",
      "values": {"DCT4": "SO"}}, "not in company"),
    ({"capability_id": "document_type_definition", "table": "F40039", "key": {"DCTO": "SW"}, "action": "add",
      "values": {"DCT4": "ST"}}, "not an allowed value"),
    ({"capability_id": "document_type_definition", "table": "F40205", "key": {"LNTY": "W"}, "action": "add",
      "values": {"LNDS": "x"}}, "F40039 only"),
    ({"capability_id": "udc_value_maintenance", "product_code": "00", "udc_type": "DT", "code": "SW", "action": "update",
      "values": {"DRHRDC": "N"}}, "cannot be changed here"),
    ({"capability_id": "batch_version_data_selection", "application": "R42565", "version": "XJDE0001",
      "specification": "x"}, "Oracle-owned"),
    ({"capability_id": "udc_value_maintenance", "product_code": "00", "udc_type": "DT", "code": "SW", "action": "delete",
      "values": {"DRDL01": "x"}}, "never deletes"),
])
def test_an_item_outside_the_rules_or_the_scope_is_refused_when_proposed(ready, change, reason):
    from jde_mcp_server import approval
    from jde_mcp_server.scope import ScopeViolation

    with pytest.raises((approval.ChangeApprovalError, ScopeViolation)) as exc:
        _propose("S-CS-BAD", {"tool": "configuration_change_set", "items": [change]})
    assert reason in str(exc.value)


def test_a_protected_or_never_touch_category_is_refused(client):
    ready_company(client, "vdb", approvedReads=READS)
    scope = {**SCOPE, "functionalAgent": {**SCOPE["functionalAgent"], "neverTouchCategories": ["document_and_order_types"]}}
    _save_scope(client, "vdb", scope)
    from jde_mcp_server import approval
    from jde_mcp_server.scope import ScopeViolation

    with pytest.raises(ScopeViolation) as exc:
        _propose("S-CS-NEVER", {"tool": "configuration_change_set", "items": [NEW_ORDER_TYPE["items"][0]]})
    assert "never-touch" in str(exc.value)


def test_adding_a_row_that_already_exists_is_refused_at_approval(ready):
    from jde_mcp_server.binding import BindingInvalid

    apply_row_in_dev("vdb", "F40039", {"DCTO": "SW"}, {"DCT4": "SO", "DCDL01": "exists already"})
    rec = _propose("S-CS-EXISTS", {"tool": "configuration_change_set", "items": [NEW_ORDER_TYPE["items"][1]]})
    with pytest.raises(BindingInvalid) as exc:
        _approve(rec["change_id"])
    assert "already exists" in str(exc.value)


def test_the_preflight_reports_every_item(ready):
    from jde_mcp_server import approval

    rec = _propose("S-CS-PRE")
    _approve(rec["change_id"])
    report = approval.preflight(rec["change_id"])
    names = [c["check"] for c in report["checks"]]
    assert sum("inside the company's approved scope" in n for n in names) == 4
    assert report["executable"], [c for c in report["checks"] if not c["ok"]]


def test_a_legacy_single_processing_option_change_still_works(ready):
    """Changes proposed before change sets existed are a set of one item."""
    from jde_mcp_server import approval

    _approved_story("S-CS-LEGACY")
    rec = approval.propose_change("S-CS-LEGACY", {"tool": "set_processing_option", "application": "P4210",
                                                  "version": "CIQ0001", "option": "PDOCTYPE", "value": "SO"},
                                  "processing_option_update")
    _approve(rec["change_id"])
    apply_in_dev("vdb", "P4210", "CIQ0001", "PDOCTYPE", "SO")
    r = _record(ready, "S-CS-LEGACY")
    assert r.status_code == 200 and r.json()["observedValue"] == "SO"
