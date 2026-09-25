"""
Stage 1 increment S1-2: execution safeguards.

Each test states one way execution must be refused. The gate is
exercised directly (mcp_server's approval/ais_client, in mock mode --
no JDE call is ever made) with the company records written through the
real Admin API, so what an Admin saves is exactly what is enforced.

  * Company scope: a story's company comes from its intake link; that
    company's own saved scope is the only one consulted. No link, no
    saved scope, or a different company all refuse.
  * Approval authority: the company's approval policy names which roles
    may approve. No policy, a policy the gate does not understand, or an
    approver without an allowed role all refuse -- at approval AND again
    at execution (a policy tightened after approval also refuses).
  * Expiring windows: an approval past its policy-set validity, or a
    spike experiment past (or without) its expiry, allows nothing; this
    includes test runs, not just writes.
  * Run recovery: runs a restart interrupted are marked failed on
    startup instead of appearing to run forever.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from jde_mcp_server import backlog

from .conftest import TEST_PASSWORD, _apply_csrf_header, headers

OPERATION = {
    "tool": "set_processing_option", "application": "P4210", "version": "CIQ0001", "option": "PDOCTYPE", "value": "SO",
}
POLICY = {"policyVersion": 1, "exactChangeApproverRoles": ["product_manager"], "approvalValidHours": 24}


def _full_scope(spike_expires_at: str | None = "2099-01-01T00:00:00+00:00", policy: dict | None = POLICY) -> dict:
    """Everything the gate needs for OPERATION to execute in mock mode."""
    spikes = []
    if spike_expires_at is not None:
        spikes.append({
            "capabilityId": "processing_option_update", "capabilityRevision": "r1",
            "application": "P4210", "version": "CIQ0001", "option": "PDOCTYPE", "environment": "DEV",
            "expiresAt": spike_expires_at, "note": "bounded DEV test window",
        })
    body = {
        "toolsRelease": "9.2.7",
        "environment": {
            "devEnvironmentId": "JDV920", "devPathCode": "DV920", "aisDataSourceName": "Business Data - DEV",
            "isolationConfirmed": True, "isolationEvidence": "OCM mappings reviewed",
        },
        "functionalAgent": {
            "approvedVersions": [{
                "capabilityId": "processing_option_update", "optionCategory": "document_and_order_types",
                "application": "P4210", "version": "CIQ0001", "options": ["PDOCTYPE"], "allowedValues": ["SO"],
            }],
            "spikeExperiments": spikes,
        },
    }
    if policy is not None:
        body["approvalPolicy"] = policy
    # Enforced capability boundaries (capability_catalog enforcement contract).
    body["mechanismsAllowed"] = ["ais_form_service_request", "ais_orchestration"]
    body["testScope"] = {"approvedTests": [
        {"orchestration": "ORCH_SO", "sideEffects": ["creates_dev_transaction"], "note": "creates one DEV sales order"},
    ]}
    return body


def _save_scope(client, company: str, body: dict) -> dict:
    current = client.get("/admin/engagement-scope", headers=headers(company)).json()["revision"]
    r = client.put("/admin/engagement-scope", headers=headers(company), json={**body, "expectedRevision": current})
    assert r.status_code == 200, r.text
    return r.json()


def _approved_story(story_id: str, company: str | None = "vdb") -> None:
    from jde_api_service.services.registry import get_customer_link_service, get_delivery_queue_service

    backlog.propose_to_backlog(story_id, "As a clerk I want SO as the default order type", {}, "Low", source="Business")
    backlog.approve(story_id, "Ellen Vos", "fine")
    if company:
        get_customer_link_service().link(story_id, company)
        get_delivery_queue_service().add(story_id, company, "Hendro", "queued")


def _propose(story_id: str) -> dict:
    from jde_mcp_server import approval

    return approval.propose_change(story_id, {**OPERATION, "story_id": story_id}, "processing_option_update")


def _approve(change_id: str, company: str = "vdb", roles=("product_manager",)) -> dict:
    from jde_mcp_server import approval

    return approval.approve_change(
        change_id, "Hendro", company_id=company, approver_roles=set(roles), approver_user_id="u-hendro", note="ok"
    )


def _execute(story_id: str, change_id: str, value: str = "SO") -> dict:
    from jde_mcp_server.ais_client import client as ais

    return ais.set_processing_option(story_id, change_id, "P4210", "CIQ0001", "PDOCTYPE", value)


def _edit_change_record(change_id: str, **fields) -> None:
    from jde_mcp_server import approval

    record = approval._load(change_id)
    record.update(fields)
    approval._save(change_id, record)


# ---------------------------------------------------------------------
# The happy path, so every refusal below is a refusal of one thing only.
# ---------------------------------------------------------------------
def test_a_fully_configured_company_can_execute_in_mock_mode(client):
    _save_scope(client, "vdb", _full_scope())
    _approved_story("S12-OK")
    change = _propose("S12-OK")
    assert change["company_id"] == "vdb"
    approved = _approve(change["change_id"])
    assert approved["approver_authority"]["roles"] == ["product_manager"]
    assert approved["expires_at"] - approved["approved_at"] == pytest.approx(24 * 3600)
    result = _execute("S12-OK", change["change_id"])
    assert result["mock"] is True


# ---------------------------------------------------------------------
# Per-company scope
# ---------------------------------------------------------------------
def test_a_story_with_no_company_link_cannot_even_be_proposed(client):
    from jde_mcp_server.scope import ScopeViolation

    _approved_story("S12-UNLINKED", company=None)
    with pytest.raises(ScopeViolation, match="not linked to a company"):
        _propose("S12-UNLINKED")


def test_another_companys_scope_never_authorises_this_company(client):
    from jde_mcp_server.scope import ScopeViolation

    _save_scope(client, "vdb", _full_scope())  # vdb is fully configured...
    _approved_story("S12-NHD", company="nhd")  # ...but this story is nhd's, and nhd has nothing saved
    change = _propose("S12-NHD")
    with pytest.raises(ScopeViolation, match="no saved engagement scope"):
        _approve(change["change_id"], company="nhd")


def test_approval_cannot_be_given_from_another_company(client):
    from jde_mcp_server.approval import ChangeApprovalError

    _save_scope(client, "vdb", _full_scope())
    _save_scope(client, "nhd", _full_scope())
    _approved_story("S12-CROSS")
    change = _propose("S12-CROSS")
    with pytest.raises(ChangeApprovalError, match="no such change for company nhd"):
        _approve(change["change_id"], company="nhd")


def test_a_story_moved_to_another_company_after_approval_is_refused(client):
    from jde_api_service.services.registry import get_customer_link_service
    from jde_mcp_server.approval import ChangeApprovalError

    _save_scope(client, "vdb", _full_scope())
    _save_scope(client, "nhd", _full_scope())
    _approved_story("S12-MOVED")
    change = _propose("S12-MOVED")
    _approve(change["change_id"])
    get_customer_link_service().link("S12-MOVED", "nhd")
    with pytest.raises(ChangeApprovalError, match="was recorded for company 'vdb'"):
        _execute("S12-MOVED", change["change_id"])


def test_an_unconfirmed_dev_binding_blocks_execution(client):
    from jde_mcp_server.scope import ScopeViolation

    body = _full_scope()
    body["environment"]["isolationConfirmed"] = False
    _save_scope(client, "vdb", body)
    _approved_story("S12-ISO")
    change = _propose("S12-ISO")
    _approve(change["change_id"])
    with pytest.raises(ScopeViolation, match="does not confirm DEV isolation"):
        _execute("S12-ISO", change["change_id"])


# ---------------------------------------------------------------------
# Approval authority
# ---------------------------------------------------------------------
def test_no_approval_policy_blocks_approval_through_the_api(client):
    _save_scope(client, "vdb", _full_scope(policy=None))
    _approved_story("S12-NOPOLICY")
    _propose("S12-NOPOLICY")
    r = client.post("/changes/S12-NOPOLICY/approve-change", headers=headers("vdb"), json={"note": "ok"})
    assert r.status_code == 409
    assert "no approval policy" in r.json()["detail"]


def test_approval_through_the_api_records_authority_from_the_session(client):
    from jde_mcp_server import approval

    _save_scope(client, "vdb", _full_scope())
    _approved_story("S12-API")
    change = _propose("S12-API")
    r = client.post("/changes/S12-API/approve-change", headers=headers("vdb"), json={"note": "ok"})
    assert r.status_code == 200, r.text
    record = approval._load(change["change_id"])
    assert record["approved_by"] == "Hendro"
    assert record["approver_authority"]["roles"] == ["product_manager"]


def test_an_approver_without_an_allowed_role_is_refused_through_the_api(client):
    from fastapi.testclient import TestClient
    from jde_api_service.main import app
    from jde_api_service.services import auth_service, membership_service

    _save_scope(client, "vdb", _full_scope())  # policy: product_manager only
    _approved_story("S12-ROLE")
    _propose("S12-ROLE")

    auth_service.create_user("owner@test.local", TEST_PASSWORD, "Olga Owner", user_id="u-owner")
    membership_service.create_membership("u-owner", "vdb", ["domain_owner"], created_by="u-hendro")
    owner = TestClient(app)
    assert owner.post("/auth/login", json={"email": "owner@test.local", "password": TEST_PASSWORD}).status_code == 200
    _apply_csrf_header(owner)

    r = owner.post("/changes/S12-ROLE/approve-change", headers=headers("vdb"), json={"note": "ok"})
    assert r.status_code == 403
    assert "does not hold a role" in r.json()["detail"]


def test_a_policy_the_gate_does_not_understand_blocks_execution(client, isolated_dirs):
    from jde_mcp_server.scope import ScopeViolation

    _save_scope(client, "vdb", _full_scope())
    _approved_story("S12-UNKNOWN")
    change = _propose("S12-UNKNOWN")
    _approve(change["change_id"])

    # A newer policy shape written by something this gate predates.
    path = os.path.join(isolated_dirs["api_data_dir"], "engagement_scope", "vdb.json")
    doc = json.load(open(path))
    doc["approval_policy"]["policy_version"] = 2
    json.dump(doc, open(path, "w"))
    with pytest.raises(ScopeViolation, match="not one this gate understands"):
        _execute("S12-UNKNOWN", change["change_id"])

    doc["approval_policy"]["policy_version"] = 1
    doc["approval_policy"]["require_second_approver"] = True
    json.dump(doc, open(path, "w"))
    with pytest.raises(ScopeViolation, match="does not understand"):
        _execute("S12-UNKNOWN", change["change_id"])


def test_a_policy_tightened_after_approval_blocks_execution(client):
    from jde_mcp_server.approval import ChangeApprovalError

    _save_scope(client, "vdb", _full_scope())
    _approved_story("S12-TIGHTENED")
    change = _propose("S12-TIGHTENED")
    _approve(change["change_id"])  # approved as product_manager
    _save_scope(client, "vdb", _full_scope(policy={**POLICY, "exactChangeApproverRoles": ["admin"]}))
    with pytest.raises(ChangeApprovalError, match="current approval policy"):
        _execute("S12-TIGHTENED", change["change_id"])


def test_the_api_refuses_an_approval_policy_it_cannot_enforce(client):
    for bad in (
        {**POLICY, "exactChangeApproverRoles": []},
        {**POLICY, "exactChangeApproverRoles": ["dashboard_viewer"]},
        {**POLICY, "approvalValidHours": 0},
        {**POLICY, "approvalValidHours": 1000},
        {**POLICY, "policyVersion": 2},
    ):
        r = client.put("/admin/engagement-scope", headers=headers("vdb"), json={"approvalPolicy": bad})
        assert r.status_code == 422, bad


# ---------------------------------------------------------------------
# Expiring windows
# ---------------------------------------------------------------------
def test_an_expired_approval_blocks_execution_and_test_runs(client):
    from jde_mcp_server.ais_client import client as ais
    from jde_mcp_server.approval import ChangeApprovalError

    _save_scope(client, "vdb", _full_scope())
    _approved_story("S12-EXPIRED")
    change = _propose("S12-EXPIRED")
    _approve(change["change_id"])
    _edit_change_record(change["change_id"], expires_at=time.time() - 1)
    with pytest.raises(ChangeApprovalError, match="expired"):
        _execute("S12-EXPIRED", change["change_id"])
    with pytest.raises(ChangeApprovalError, match="expired"):
        ais.run_orchestration("S12-EXPIRED", change["change_id"], "", {})


def test_an_approval_without_an_expiry_blocks_execution(client):
    from jde_mcp_server.approval import ChangeApprovalError

    _save_scope(client, "vdb", _full_scope())
    _approved_story("S12-NOEXPIRY")
    change = _propose("S12-NOEXPIRY")
    _approve(change["change_id"])
    _edit_change_record(change["change_id"], expires_at=None)
    with pytest.raises(ChangeApprovalError, match="no approval expiry"):
        _execute("S12-NOEXPIRY", change["change_id"])


def test_an_expired_spike_experiment_allows_nothing(client):
    from jde_mcp_server.capability_catalog import CapabilityError

    _save_scope(client, "vdb", _full_scope(spike_expires_at="2020-01-01T00:00:00+00:00"))
    _approved_story("S12-SPIKE-OLD")
    change = _propose("S12-SPIKE-OLD")
    _approve(change["change_id"])
    with pytest.raises(CapabilityError):
        _execute("S12-SPIKE-OLD", change["change_id"])


def test_an_undated_spike_experiment_is_refused_on_save(client):
    body = _full_scope()
    del body["functionalAgent"]["spikeExperiments"][0]["expiresAt"]
    assert client.put("/admin/engagement-scope", headers=headers("vdb"), json=body).status_code == 422
    body["functionalAgent"]["spikeExperiments"][0]["expiresAt"] = "2099-01-01T00:00:00"  # no timezone
    assert client.put("/admin/engagement-scope", headers=headers("vdb"), json=body).status_code == 422


def test_a_spike_for_a_different_capability_revision_allows_nothing(client, isolated_dirs):
    from jde_mcp_server.capability_catalog import CapabilityError

    body = _full_scope()
    body["functionalAgent"]["spikeExperiments"][0]["capabilityRevision"] = "r0"
    _save_scope(client, "vdb", body)
    _approved_story("S12-SPIKE-REV")
    change = _propose("S12-SPIKE-REV")
    _approve(change["change_id"])
    with pytest.raises(CapabilityError):
        _execute("S12-SPIKE-REV", change["change_id"])


def test_spike_approval_is_stamped_by_the_server(client):
    body = _full_scope()
    body["functionalAgent"]["spikeExperiments"][0]["approvedBy"] = "Somebody Else"
    saved = _save_scope(client, "vdb", body)
    assert saved["functionalAgent"]["spikeExperiments"][0]["approvedBy"] == "Hendro"


# ---------------------------------------------------------------------
# Run recovery
# ---------------------------------------------------------------------
def test_runs_interrupted_by_a_restart_are_marked_failed_on_startup(client):
    from fastapi.testclient import TestClient
    from jde_api_service.main import app
    from jde_api_service.services.registry import (
        get_agent_run_service,
        get_architecture_review_service,
        get_enhancement_run_service,
    )
    from jde_api_service.services.run_recovery import INTERRUPTED

    enhancement = get_enhancement_run_service()
    enhancement.start("CR-INTERRUPTED")
    enhancement.set_stage("CR-INTERRUPTED", "improving")
    enhancement.start("CR-FINISHED")
    enhancement.fail("CR-FINISHED", "a real, earlier failure")
    get_architecture_review_service().start("S-INTERRUPTED")
    agent_run = get_agent_run_service().start(
        agent_name="architect", driver="architecture_driver", story_id="S-INTERRUPTED", customer_id="vdb"
    )

    with TestClient(app):  # a restart: the startup lifespan runs again
        pass

    assert enhancement.get("CR-INTERRUPTED").stage == "failed"
    assert enhancement.get("CR-INTERRUPTED").error == INTERRUPTED
    assert enhancement.get("CR-FINISHED").error == "a real, earlier failure"  # untouched
    review = get_architecture_review_service().get("S-INTERRUPTED")
    assert (review.stage, review.error) == ("failed", INTERRUPTED)
    run = next(r for r in get_agent_run_service().list_all() if r.run_id == agent_run.run_id)
    assert (run.stage, run.error) == ("failed", INTERRUPTED)
