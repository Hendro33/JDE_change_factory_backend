"""
Functional Agent design update -- the Capability Catalogue read
surface (GET /admin/capabilities) and its projection onto a Change's
exactChange (capabilityId/capabilityStatus/capabilityExecutable).

Both are read-only over the SAME source of truth mcp_server's own
prove_the_gate.py exercises directly (capability_catalog.json,
approval.py) -- these tests confirm the API layer surfaces that
correctly, not that the underlying gate logic works (that's
prove_the_gate.py's job).
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
    # separate fields beneath status (design update Section 2) -- this
    # capability's gating logic is proven (prove_the_gate.py) but the
    # real JDE write is not, and that nuance must survive to the API.
    assert "prove_the_gate" in validation["technicalValidation"]
    assert validation["status"] == "needs_spike"


def test_unknown_capability_id_is_404(client):
    r = client.get("/admin/capabilities/not-a-real-capability", headers=headers(customer="vdb"))
    assert r.status_code == 404


def test_exact_change_surfaces_capability_status_and_executability(client, monkeypatch):
    """A change bound to a Needs-spike capability, fully approved at
    the story and exact-change level, must still show as NOT
    executable through the capability lens -- broader mandate does not
    mean broader execution permission (design update's closing
    principle), and the governance screen must be able to show that
    distinction, not just "approved"."""
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
    approval_module.approve_change(record["change_id"], "Hendro", note="approved for the test")

    r = client.get(f"/changes/{story_id}", headers=headers(customer="vdb"))
    assert r.status_code == 200
    ec = r.json()["exactChange"]
    assert ec["capabilityId"] == "processing_option_update"
    assert ec["capabilityStatus"] == "needs_spike"
    # Approved at both the story and exact-change level, but the
    # capability itself is Needs spike with no matching spike
    # experiment in this test's (nonexistent) scope.json -- so not
    # executable, and the API must say so explicitly rather than
    # implying "approved" means "will execute."
    assert ec["capabilityExecutable"] is False
    assert r.json()["changeApproval"]["status"] == "approved"
