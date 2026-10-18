"""
Agent permissions in the engagement scope: an Admin allows whole KINDS of
change for the Functional Agent (functional_agent.allowed_capabilities)
instead of listing every table, UDC and version. Within an allowed kind any
target passes the scope check; Jade's universal rules still hold. The
Architect reads the permissions only to route its design, and a missing
scope is reported to it as an Administrator setup item, never raised.
"""

from __future__ import annotations

import pytest

from .conftest import headers
from .test_stage1_execution_safeguards import OPERATION, _approve, _approved_story, _full_scope, _save_scope


def _open_scope(kinds: list[str]) -> dict:
    body = _full_scope(spike_expires_at=None)
    body["functionalAgent"] = {"allowedCapabilities": kinds}
    return body


def test_allowed_kind_accepts_any_target_without_listing():
    from jde_mcp_server import capability_catalog, scope

    s = {"customer_id": "vdb", "functional_agent": {"allowed_capabilities": ["udc_value_maintenance",
                                                                             "processing_option_update"]}}
    udc = {"kind": "udc_value", "capability_id": "udc_value_maintenance", "product_code": "56", "user_defined_codes": "GR",
           "code": "WPC80", "values": {"DRDL01": "Whey protein 80"}, "action": "add"}
    assert scope.check_item_scope(s, udc, capability_catalog.require_enforcement("udc_value_maintenance"))["open"]
    po = {"kind": "processing_option", "capability_id": "processing_option_update", "application": "P4210",
          "version": "HWG0001", "option": "PDOCTYPE", "value": "SO"}
    assert scope.check_item_scope(s, po, capability_catalog.require_enforcement("processing_option_update"))["open"]


def test_kind_not_allowed_still_needs_a_listed_target():
    from jde_mcp_server import capability_catalog, scope

    s = {"customer_id": "vdb", "functional_agent": {"allowed_capabilities": ["udc_value_maintenance"]}}
    po = {"kind": "processing_option", "capability_id": "processing_option_update", "application": "P4210",
          "version": "HWG0001", "option": "PDOCTYPE", "value": "SO"}
    with pytest.raises(scope.ScopeViolation):
        scope.check_item_scope(s, po, capability_catalog.require_enforcement("processing_option_update"))


def test_universal_rule_holds_inside_an_allowed_kind():
    from jde_mcp_server import capability_catalog, scope

    s = {"customer_id": "vdb", "functional_agent": {"allowed_capabilities": ["processing_option_update"]}}
    po = {"kind": "processing_option", "capability_id": "processing_option_update", "application": "P4210",
          "version": "ZJDE0001", "option": "PDOCTYPE", "value": "SO"}
    with pytest.raises(scope.ScopeViolation):
        scope.check_item_scope(s, po, capability_catalog.require_enforcement("processing_option_update"))


def test_unknown_kind_is_refused_on_save(client):
    body = _open_scope(["udc_value_maintenance", "write_anything"])
    current = client.get("/admin/engagement-scope", headers=headers("vdb")).json()["revision"]
    r = client.put("/admin/engagement-scope", headers=headers("vdb"), json={**body, "expectedRevision": current})
    assert r.status_code == 422
    assert "write_anything" in r.text


def test_allowed_kinds_are_saved_and_shown(client):
    saved = _save_scope(client, "vdb", _open_scope(["udc_value_maintenance", "processing_option_update"]))
    assert saved["functionalAgent"]["allowedCapabilities"] == ["udc_value_maintenance", "processing_option_update"]
    from jde_mcp_server import scope

    described = scope.describe_scope("vdb")
    assert described["saved"] is True
    assert described["functional_agent"]["allowed_kinds"] == ["udc_value_maintenance", "processing_option_update"]
    assert "never what the Architect may explore" in described["routing_note"]


def test_missing_scope_is_described_not_raised():
    from jde_mcp_server import scope

    described = scope.describe_scope("no-such-company")
    assert described["saved"] is False
    assert "Administrator" in described["routing_note"]


def test_open_kind_is_proposed_approved_and_passes_preflight(client):
    """End to end on the gate: a processing option on a version nobody listed
    is proposed, approved and preflighted when its kind is allowed."""
    from jde_mcp_server import approval

    _save_scope(client, "vdb", _open_scope(["processing_option_update"]))
    _approved_story("CR-OPEN-1")
    op = {**OPERATION, "version": "HWG0007", "story_id": "CR-OPEN-1"}
    change = approval.propose_change("CR-OPEN-1", op, "processing_option_update")
    _approve(change["change_id"])
    pre = approval.preflight(change["change_id"])
    scope_checks = [c for c in pre["checks"] if "approved versions" in c["check"]]
    assert scope_checks and all(c["ok"] for c in scope_checks), pre["checks"]
