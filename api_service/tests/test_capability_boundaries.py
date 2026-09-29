"""
Enforced capability boundaries for delivering an approved change.

The deliverable capabilities are enumerated: only a capability whose
catalogue entry carries a complete, machine-readable enforcement contract
(tool, mechanism, target, option categories with a protected flag, test
mechanism and permitted test side effects) can be delivered. Today that is
processing_option_update alone.

For it, the delivery gate (approval.authorise_functional_delivery, run
before a person's "applied in DEV" is recorded) enforces -- against closed
values in the company's saved scope, never against free text:

  * target:    the approved entry must be approved FOR this capability;
  * category:  the option's category must be declared, known, not
               protected by the capability and not never-touch for the
               company.

The company's mechanism allow-list governs what Jade itself runs in JDE:
a person applying the change is not an AIS mechanism, so it does not gate
recording the write, but the live test orchestration (scope.
check_test_boundary) needs:

  * test:      an approved test whose declared side effects the capability
               permits, run through an allowed mechanism.

A restriction that exists only as documentation or as a displayed label
blocks delivery instead of being assumed.
"""

from __future__ import annotations

import json
import os

import pytest

from ._discovery import ready_company
from .conftest import headers
from .test_stage1_execution_safeguards import (
    OPERATION,
    _approve,
    _approved_story,
    _execute,
    _full_scope,
    _save_scope,
)


TEST_OP = {**OPERATION, "test_orchestration": "ORCH_SO"}


def _scope(**overrides) -> dict:
    body = _full_scope()
    fa = body["functionalAgent"]
    entry = fa["approvedVersions"][0]
    for key, value in overrides.items():
        if key in ("optionCategory", "capabilityId"):
            entry[key] = value
        elif key in ("neverTouchCategories", "neverTouchNotes"):
            fa[key] = value
        else:
            body[key] = value
    return body


def _approved_change(client, story: str, scope: dict, operation: dict | None = None) -> dict:
    """An approved change. One that names a test orchestration is proposed
    with the company's JDE connection ready: a live test needs it, and the
    target's before-state is then read live at approval."""
    from jde_mcp_server import approval

    if operation is not None and operation.get("test_orchestration"):
        ready_company(client, "vdb")
    _save_scope(client, "vdb", scope)
    _approved_story(story)
    op = {**(operation or OPERATION), "story_id": story}
    change = approval.propose_change(story, op, "processing_option_update")
    _approve(change["change_id"])
    return change


def _run_test(change_id: str) -> dict:
    """Run the change's approved test orchestration live (delivery/functional.run_test)."""
    from jde_api_service.delivery import functional

    return functional.run_test(change_id, actor_user_id="u-hendro", actor_name="Hendro")


# ---------------------------------------------------------------------
# Enumeration
# ---------------------------------------------------------------------
def test_the_eight_configuration_capabilities_are_executable():
    from jde_mcp_server import capability_catalog

    contracts = capability_catalog.executable_capabilities()
    assert set(contracts) == {"processing_option_update", "udc_value_maintenance", "constants_and_setup_master_data",
                              "document_type_definition", "line_type_definition", "order_activity_status_rules",
                              "batch_version_data_selection", "batch_version_data_sequencing"}
    assert {c["item_kind"] for c in contracts.values()} == {
        "processing_option", "udc_value", "setup_row", "version_data_selection", "version_data_sequencing"}
    for cid, c in contracts.items():
        if cid != "processing_option_update":
            assert c["tool"] == "configuration_change_set", cid
    contract = contracts["processing_option_update"]
    assert contract["tool"] == "set_processing_option"
    assert contract["mechanism"] == "ais_form_service_request"
    assert contract["test"]["mechanism"] == "ais_orchestration"
    protected = {c for c, v in contract["option_categories"].items() if v["protected"]}
    assert {"pricing", "tax", "gl_posting_and_aai", "security_and_authorisation", "payments_and_banking"} <= protected
    assert set(contract["test"]["permitted_side_effects"]) == {"none", "creates_dev_transaction"}


def test_a_documented_but_unenforced_capability_cannot_even_be_proposed(client):
    from jde_mcp_server import approval

    _save_scope(client, "vdb", _full_scope())
    _approved_story("S-CB-DOC")
    op = {"tool": "set_processing_option", "story_id": "S-CB-DOC", "application": "P4210", "version": "CIQ0001",
          "option": "PDOCTYPE", "value": "SO"}
    # custom_object_text_change is delivered by the Technical Agent's package, never proposed here.
    with pytest.raises(approval.ChangeApprovalError, match="no execution adapter"):
        approval.propose_change("S-CB-DOC", op, "custom_object_text_change")
    # A configuration capability is proposed only as an item of a change set.
    with pytest.raises(approval.ChangeApprovalError, match="executes only through 'configuration_change_set'"):
        approval.propose_change("S-CB-DOC", op, "udc_value_maintenance")


def test_removing_the_contract_blocks_an_already_approved_change(client, tmp_path, monkeypatch):
    from jde_mcp_server import capability_catalog

    change = _approved_change(client, "S-CB-NOCONTRACT", _full_scope())
    catalog = json.load(open(capability_catalog.CATALOG_FILE))
    for cap in catalog["capabilities"]:
        if cap["capability_id"] == "processing_option_update":
            del cap["enforcement"]["option_categories"]["pricing"]["protected"]  # now incomplete
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(catalog))
    monkeypatch.setattr(capability_catalog, "CATALOG_FILE", str(path))
    with pytest.raises(Exception, match="incomplete enforcement contract"):
        _execute("S-CB-NOCONTRACT", change["change_id"])


# ---------------------------------------------------------------------
# Target and mechanism
# ---------------------------------------------------------------------
def test_the_mechanism_allow_list_does_not_gate_a_person_applying_the_change(client):
    """A person applying the value in DEV is not an AIS mechanism: the write
    is recorded even though the company allows no form-service writes."""
    from jde_mcp_server import approval, execution

    change = _approved_change(client, "S-CB-MECH", _scope(mechanismsAllowed=["ais_orchestration"]))
    assert _execute("S-CB-MECH", change["change_id"])["outcome"] == "applied"
    assert execution.effective_state(approval._load(change["change_id"]), execution.WRITE) == "applied"


def test_no_mechanisms_saved_means_jade_runs_nothing_in_jde(client):
    from jde_mcp_server.scope import ScopeViolation

    change = _approved_change(client, "S-CB-NOMECH", _scope(mechanismsAllowed=[]), TEST_OP)
    _execute("S-CB-NOMECH", change["change_id"])
    with pytest.raises(ScopeViolation, match="allowed: none"):
        _run_test(change["change_id"])


def test_a_target_approved_for_another_capability_is_refused(client, isolated_dirs):
    from jde_mcp_server.scope import ScopeViolation

    change = _approved_change(client, "S-CB-TARGET", _full_scope())
    from jde_mcp_server import docstore

    doc = docstore.get("engagement_scope", "vdb")
    doc["functional_agent"]["approved_versions"][0]["capability_id"] = "batch_version_data_selection"
    docstore.put("engagement_scope", "vdb", doc)
    with pytest.raises(ScopeViolation, match="approved for capability 'batch_version_data_selection'"):
        _execute("S-CB-TARGET", change["change_id"])


# ---------------------------------------------------------------------
# Protected and never-touch categories
# ---------------------------------------------------------------------
@pytest.mark.parametrize("category", ["pricing", "tax", "gl_posting_and_aai", "payments_and_banking"])
def test_a_protected_category_is_never_written(client, category):
    from jde_mcp_server.scope import ScopeViolation

    change = _approved_change(client, f"S-CB-P-{category}", _scope(optionCategory=category))
    with pytest.raises(ScopeViolation, match="is protected for this capability"):
        _execute(f"S-CB-P-{category}", change["change_id"])


def test_an_undeclared_category_blocks_the_write(client):
    from jde_mcp_server.scope import ScopeViolation

    change = _approved_change(client, "S-CB-NOCAT", _scope(optionCategory=""))
    with pytest.raises(ScopeViolation, match="no declared option category"):
        _execute("S-CB-NOCAT", change["change_id"])


def test_a_never_touch_category_blocks_the_write(client):
    from jde_mcp_server.scope import ScopeViolation

    change = _approved_change(client, "S-CB-NEVER", _scope(neverTouchCategories=["document_and_order_types"]))
    with pytest.raises(ScopeViolation, match="never-touch"):
        _execute("S-CB-NEVER", change["change_id"])


def test_free_text_labels_are_kept_as_notes_and_never_enforced(client):
    saved = _save_scope(client, "vdb", _scope(neverTouchCategories=["pricing", "Order types -- please ask Finance"]))
    fa = saved["functionalAgent"]
    assert fa["neverTouchCategories"] == ["pricing"]
    assert fa["neverTouchNotes"] == ["Order types -- please ask Finance"]

    # The note mentions order types, but a note is not enforcement:
    # the write is governed by the enforced category list only.
    change = _approved_change(client, "S-CB-NOTE", _scope(neverTouchNotes=["Order types -- please ask Finance"]))
    assert _execute("S-CB-NOTE", change["change_id"])["outcome"] == "applied"


def test_the_api_refuses_unknown_categories_and_side_effects(client):
    current = client.get("/admin/engagement-scope", headers=headers("vdb")).json()["revision"]
    for body in (
        _scope(optionCategory="whatever_i_like"),
        _scope(testScope={"approvedTests": [{"orchestration": "X", "sideEffects": ["sends_email"]}]}),
        _scope(testScope={"approvedTests": [{"orchestration": "X", "sideEffects": []}]}),
        _scope(mechanismsAllowed=["direct_sql"]),
    ):
        r = client.put("/admin/engagement-scope", headers=headers("vdb"), json={**body, "expectedRevision": current})
        assert r.status_code == 422, r.text


# ---------------------------------------------------------------------
# Test boundaries
# ---------------------------------------------------------------------


def test_an_approved_test_with_permitted_side_effects_runs(client, ais):
    change = _approved_change(client, "S-CB-T-OK", _full_scope(), TEST_OP)
    _execute("S-CB-T-OK", change["change_id"])
    result = _run_test(change["change_id"])
    assert result["outcome"] == "completed" and result["orchestration"] == "ORCH_SO"
    assert any(p == "/jderest/v3/orchestrator/ORCH_SO" for _, p, _c in ais.calls)


def test_a_test_not_in_the_approved_tests_is_refused(client):
    from jde_mcp_server.scope import ScopeViolation

    change = _approved_change(client, "S-CB-T-UNAPPROVED", _scope(testScope={"approvedTests": []}), TEST_OP)
    _execute("S-CB-T-UNAPPROVED", change["change_id"])
    with pytest.raises(ScopeViolation, match="not one of this company's approved tests"):
        _run_test(change["change_id"])


@pytest.mark.parametrize("effect", ["posting", "payment", "outbound_integration", "batch_run"])
def test_a_test_with_a_forbidden_side_effect_is_refused(client, effect):
    from jde_mcp_server.scope import ScopeViolation

    scope = _scope(testScope={"approvedTests": [{"orchestration": "ORCH_SO", "sideEffects": ["creates_dev_transaction", effect]}]})
    change = _approved_change(client, f"S-CB-T-{effect}", scope, TEST_OP)
    _execute(f"S-CB-T-{effect}", change["change_id"])
    with pytest.raises(ScopeViolation, match=f"does not permit in a test: {effect}"):
        _run_test(change["change_id"])


def test_a_test_needs_the_orchestration_mechanism_allowed(client):
    from jde_mcp_server.scope import ScopeViolation

    change = _approved_change(client, "S-CB-T-MECH", _scope(mechanismsAllowed=["ais_form_service_request"]), TEST_OP)
    _execute("S-CB-T-MECH", change["change_id"])
    with pytest.raises(ScopeViolation, match="'ais_orchestration' mechanism"):
        _run_test(change["change_id"])


def test_preflight_lists_every_boundary_and_blocks_on_a_protected_category(client):
    from jde_mcp_server import approval

    change = _approved_change(client, "S-CB-PRE", _scope(optionCategory="tax"), TEST_OP)
    result = approval.preflight(change["change_id"])
    names = {c["check"]: c for c in result["checks"]}
    assert names["Capability has a complete enforcement contract"]["ok"] is True
    # The allow-list governs the live test, not a person's write.
    assert not any("Mechanism allowed" in name for name in names)
    assert names["Option category declared, not protected, not never-touch"]["ok"] is False
    assert "Test is approved, its mechanism allowed, its side effects permitted" in names
    assert result["executable"] is False
