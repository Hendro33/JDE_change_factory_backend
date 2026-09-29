"""
Functional Agent design update -- the Capability Catalogue read
surface (GET /admin/capabilities) and its projection onto a Change's
exactChange (capabilityId/capabilityStatus/capabilityExecutable).

Both are read-only over the SAME source of truth the gate uses
(capability_catalog.json, approval.py). Jade never writes to JDE: an
approved change is delivered by a person (the recorded route) and verified
by Jade, so capabilityExecutable reports whether a person may deliver it
(require_deliverable_by_person: everything except Restricted/Suspended),
while automated execution (require_executable) still needs Validated.
"""

from __future__ import annotations

from jde_mcp_server import backlog

from .conftest import headers


def test_list_capabilities_returns_the_catalogue(client):
    r = client.get("/admin/capabilities", headers=headers(customer="vdb"))
    assert r.status_code == 200
    body = r.json()
    assert body["catalogRevision"]
    ids = {c["capabilityId"] for c in body["capabilities"]}
    # The seven capability families from the design update's Section 3
    # priority table (document/line types count as two sub-capabilities).
    assert ids == {
        "processing_option_update",
        "batch_version_data_selection",
        "batch_version_data_sequencing",
        "udc_value_maintenance",
        "constants_and_setup_master_data",
        "document_type_definition",
        "line_type_definition",
        "order_activity_status_rules",
        "custom_object_text_change",
    }
    # Nothing in this catalogue is Validated by its own authorship --
    # every entry defaults to Needs spike until a human promotes it.
    assert all(c["validation"]["status"] == "needs_spike" for c in body["capabilities"])


def test_get_single_capability_exposes_separate_technical_and_policy_fields(client):
    r = client.get("/admin/capabilities/processing_option_update", headers=headers(customer="vdb"))
    assert r.status_code == 200
    body = r.json()
    assert body["capabilityId"] == "processing_option_update"
    validation = body["validation"]
    # Technical validation and policy restriction are kept as two
    # separate fields beneath status (design update Section 2) -- the
    # gating logic is enforced but an automated JDE write is not validated,
    # so a person applies the value and Jade reads it back live; that
    # nuance must survive to the API.
    assert "configuration change set" in validation["technicalValidation"]
    assert "the agents apply each approved item" in validation["technicalValidation"]
    assert "read" in validation["technicalValidation"] and "live" in validation["technicalValidation"]
    assert validation["policyRestriction"]
    assert validation["status"] == "needs_spike"


def test_unknown_capability_id_is_404(client):
    r = client.get("/admin/capabilities/not-a-real-capability", headers=headers(customer="vdb"))
    assert r.status_code == 404


def test_exact_change_surfaces_capability_status_and_executability(client, monkeypatch):
    """A change bound to a Needs-spike capability, fully approved at the
    story and exact-change level, is deliverable by a person (the recorded
    route) -- capabilityExecutable says so -- while a Restricted or
    Suspended capability is not deliverable at all, whatever the approval.
    The governance screen shows the capability lens separately from
    "approved"."""
    from jde_api_service.services.registry import get_customer_link_service, get_delivery_queue_service
    from jde_mcp_server import approval as approval_module

    story_id = "S-CAPABILITY-SURFACE"
    backlog.propose_to_backlog(story_id, "As a clerk I want X", {}, "Low", source="Business")
    backlog.approve(story_id, "Ellen Vos", "fine")
    get_customer_link_service().link(story_id, "vdb")
    get_delivery_queue_service().add(story_id, "vdb", "Hendro", "queued")

    record = approval_module.propose_change(
        story_id,
        {"tool": "set_processing_option", "application": "P4210", "version": "CIQ0001", "option": "PDOCTYPE", "value": "SO"},
        "processing_option_update",
    )
    # An Admin sets the company's approval policy -- without one nothing can be approved.
    r = client.put(
        "/admin/engagement-scope",
        headers=headers(customer="vdb"),
        json={"approvalPolicy": {"policyVersion": 1, "exactChangeApproverRoles": ["product_manager"], "approvalValidHours": 24}},
    )
    assert r.status_code == 200, r.text
    approval_module.approve_change(
        record["change_id"], "Hendro", company_id="vdb", approver_roles={"product_manager"},
        approver_user_id="u-hendro", note="approved for the test",
    )

    r = client.get(f"/changes/{story_id}", headers=headers(customer="vdb"))
    assert r.status_code == 200
    ec = r.json()["exactChange"]
    assert ec["capabilityId"] == "processing_option_update"
    assert ec["capabilityStatus"] == "needs_spike"
    # Needs spike blocks automated execution, not a person's delivery.
    assert ec["capabilityExecutable"] is True
    assert r.json()["changeApproval"]["status"] == "approved"

    # The same approved change under a Restricted / Suspended capability is
    # not deliverable, and the API says so rather than implying "approved".
    from jde_mcp_server import capability_catalog

    real = capability_catalog.get_capability
    for status in ("restricted", "suspended"):
        def patched(cid, _status=status):
            cap = real(cid)
            return None if cap is None else {**cap, "validation": {**cap["validation"], "status": _status}}

        monkeypatch.setattr(capability_catalog, "get_capability", patched)
        ec = client.get(f"/changes/{story_id}", headers=headers(customer="vdb")).json()["exactChange"]
        assert ec["capabilityStatus"] == status
        assert ec["capabilityExecutable"] is False
        assert r.json()["changeApproval"]["status"] == "approved"
    monkeypatch.setattr(capability_catalog, "get_capability", real)


def test_person_delivery_and_automated_execution_are_separate_gates():
    """require_deliverable_by_person (the recorded route) admits Needs spike;
    require_executable (automated execution) does not without an approved
    spike experiment. Both keep the revision and DEV-only rules, and neither
    delivers a Restricted or Suspended capability."""
    import pytest

    from jde_mcp_server import capability_catalog as cc

    rev = cc.require_capability("processing_option_update")["revision"]
    assert cc.require_deliverable_by_person("processing_option_update", rev, "DEV")["capability_id"] == "processing_option_update"
    with pytest.raises(cc.CapabilityError, match="Needs spike"):
        cc.require_executable("processing_option_update", rev, "DEV")
    for gate in (cc.require_deliverable_by_person, cc.require_executable):
        with pytest.raises(cc.CapabilityError, match="DEV"):
            gate("processing_option_update", rev, "PY")
        with pytest.raises(cc.CapabilityError, match="revision"):
            gate("processing_option_update", rev + "-stale", "DEV")
    assert cc.PERSON_DELIVERY_BLOCKED_STATUSES == {"restricted", "suspended"}


def test_custom_object_text_change_is_prepared_by_jade_and_applied_by_a_person():
    """Jade prepares C source (never ER text) and a person applies it through
    OMW; there is no simulated ER adapter any more."""
    from jde_mcp_server import capability_catalog as cc

    cap = cc.require_capability("custom_object_text_change")
    te = cap["technical_enforcement"]
    assert te["formats"]["c_source"]["prepare"] is True and te["formats"]["c_source"]["apply"] == {"recorded": True}
    assert te["formats"]["er_text"]["prepare"] is False and te["formats"]["er_text"]["apply"] == {"recorded": False}
    assert "jade_sim_er" not in te["formats"]
    assert list(te["adapters"]) == ["recorded"]
    assert te["adapters"]["recorded"]["available"] is True
    assert te["adapters"]["recorded"]["adapter"] == "person_through_omw"
    assert te["object_types"] == ["BSFN", "ER"]
