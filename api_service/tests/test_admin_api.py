"""
Tests for the Administration area: Customer Setup, ERP / JDE Landscape,
Agents (definition + health), Business Domains (write path) and
Integrations -- plus the small underlying capabilities this increment
adds (customer-scoped EngagementScope, agent-run history, decision
feedback with identity attribution, and UserStory.revision_count being
maintained through a Domain Owner edit cycle).
"""

from __future__ import annotations

import json

import claude_agent_sdk as sdk

from jde_mcp_server import backlog

from .conftest import headers
from .test_domain_governance import _fake_enhance_success, _result, _seed_and_enhance_t001


def test_customer_profile_is_customer_scoped(client, ellen_client):
    r = client.get("/admin/customer-profile", headers=headers(customer="vdb"))
    assert r.status_code == 200
    body = r.json()
    assert body["customer"]["id"] == "vdb"
    # Ellen (u-ellen) is only entitled to vdb -- she must appear here.
    ids = {i["id"] for i in body["identities"]}
    assert "u-ellen" in ids

    # A customer outside the caller's entitlement is refused, not leaked.
    r = ellen_client.get("/admin/customer-profile", headers=headers(customer="nhd"))
    assert r.status_code == 403


def test_erp_landscape_never_exposes_credentials(client):
    r = client.get("/admin/erp-landscape", headers=headers(customer="vdb"))
    assert r.status_code == 200
    body = r.json()
    assert body["customerId"] == "vdb"
    assert "mockMode" in body["ais"]
    assert "baseUrlConfigured" in body["ais"]
    # Never a username, password or token anywhere in the response.
    dumped = str(body).lower()
    for forbidden in ("password", "username", "token"):
        assert forbidden not in dumped
    assert "not yet customer-specific" in body["scopeGloballySharedNote"]


def test_engagement_scope_is_customer_scoped_and_editable(client):
    r = client.get("/admin/engagement-scope", headers=headers(customer="vdb"))
    assert r.status_code == 200
    assert r.json()["functionalAgent"]["approvedVersions"] == []
    assert r.json()["updatedAt"] is None

    payload = {
        "toolsRelease": "9.2.7",
        "functionalAgent": {
            "approvedVersions": [
                {"application": "P4210", "version": "CIQ0001", "options": ["PDOCTYPE"], "allowedValues": ["SO", "SV"], "notes": ""}
            ],
            "neverTouchCategories": ["tax calculation processing options"],
            "approvers": ["Ellen Vos, Application Manager"],
        },
        "technicalAgent": {"authorizedObjectTypes": [], "reservedProductCode": "", "namingPrefix": "", "approvers": []},
        "updatedBy": "Hendro",
    }
    r = client.put("/admin/engagement-scope", headers=headers(customer="vdb"), json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["functionalAgent"]["approvedVersions"][0]["application"] == "P4210"
    assert body["updatedBy"] == "Hendro"
    assert body["updatedAt"]

    # Scoped: nhd must not see vdb's scope.
    r = client.get("/admin/engagement-scope", headers=headers(customer="nhd"))
    assert r.json()["functionalAgent"]["approvedVersions"] == []


def test_agents_list_reflects_the_real_md_files(client):
    r = client.get("/admin/agents", headers=headers())
    assert r.status_code == 200
    names = {a["name"] for a in r.json()}
    assert names == {"architect", "check-agent", "functional-agent", "improve-agent", "receive-agent"}
    architect = next(a for a in r.json() if a["name"] == "architect")
    assert "mcp__jde-change-factory__get_approved_story" in architect["declaredTools"]
    assert len(architect["version"]) == 12  # short content hash
    assert architect["runtime"]["driver"] == "architecture_driver"
    assert architect["runtime"]["maxTurns"] == 40

    # functional-agent has no api_service driver wiring today -- honest, not fabricated.
    functional = next(a for a in r.json() if a["name"] == "functional-agent")
    assert functional["runtime"] is None


def test_agent_health_starts_empty(client):
    r = client.get("/admin/agents/architect/health", headers=headers())
    assert r.status_code == 200
    body = r.json()
    assert body["agentName"] == "architect"
    assert body["recentRuns"] == []
    assert body["runCounts"] == {}


def test_business_domain_create_and_status_transition(client):
    r = client.post(
        "/admin/business-domains",
        headers=headers(customer="vdb"),
        json={"apqcCode": "4.4.1", "name": "Order Fulfilment", "level": "4.4.1", "description": "", "domainOwner": ""},
    )
    assert r.status_code == 201
    domain_id = r.json()["id"]
    assert r.json()["status"] == "active"

    # Newly created domain must be customer-scoped -- invisible to another customer.
    r = client.get("/business-domains", headers=headers(customer="nhd"))
    assert domain_id not in [d["id"] for d in r.json()]

    r = client.put(
        f"/admin/business-domains/{domain_id}/status", headers=headers(customer="vdb"), json={"status": "retired"}
    )
    assert r.status_code == 200
    assert r.json()["status"] == "retired"

    # Cross-customer status edit must 404, not silently succeed.
    r = client.put(
        f"/admin/business-domains/{domain_id}/status", headers=headers(customer="nhd"), json={"status": "active"}
    )
    assert r.status_code == 404


def test_integrations_status_is_honest_about_what_is_not_connected(client):
    r = client.get("/admin/integrations", headers=headers())
    assert r.status_code == 200
    by_name = {i["name"]: i for i in r.json()}
    assert by_name["JD Edwards (AIS)"]["connected"] is False  # mock mode in tests
    assert by_name["Topdesk"]["connected"] is False
    assert by_name["Slack / Teams approvals"]["connected"] is False


def test_domain_owner_approval_records_identity_and_feedback(client, monkeypatch):
    monkeypatch.setattr(sdk, "query", _fake_enhance_success)
    r = client.post(
        "/change-requests",
        headers=headers(customer="bwm"),
        json={
            "title": "Default delivery date is wrong",
            "businessSource": "Support / Topdesk",
            "sourceReference": "Topdesk T900",
            "rawContent": "raw",
        },
    )
    change_id = r.json()["id"]
    client.post(f"/changes/{change_id}/enhance", headers=headers(customer="bwm"))

    client.get(f"/changes/{change_id}/domain-review", headers=headers(customer="bwm"))
    client.post(f"/changes/{change_id}/domain-review/start", headers=headers(customer="bwm"), json={"decidedBy": "Ellen Vos"})
    r = client.post(
        f"/changes/{change_id}/domain-review/approve", headers=headers(customer="bwm"), json={"decidedBy": "Ellen Vos"}
    )
    assert r.status_code == 200
    assert r.json()["domainOwnerApproval"]["identityId"] == "u-hendro"  # headers() defaults to u-hendro


def test_reject_exact_change_captures_structured_reason(client):
    # Seed a story straight to an approved, queued, change-pending state
    # via the real mcp_server functions (mirrors test_architecture_review.py's convention).
    from jde_mcp_server import approval as approval_module

    story_id = "S-ADMIN-REJECT"
    backlog.propose_to_backlog(story_id, "As a clerk I want X", {}, "Low", source="Business")
    backlog.approve(story_id, "Ellen Vos", "fine")
    from jde_api_service.services.registry import get_customer_link_service, get_delivery_queue_service

    get_customer_link_service().link(story_id, "vdb")
    get_delivery_queue_service().add(story_id, "vdb", "Hendro", "queued")
    record = approval_module.propose_change(
        story_id, {"tool": "set_processing_option", "application": "P4210", "version": "CIQ0001", "option": "PDOCTYPE", "value": "SO"}
    )

    r = client.post(
        f"/changes/{story_id}/reject-change",
        headers=headers(customer="vdb"),
        json={"decidedBy": "Hendro", "note": "wrong version", "rejectionReason": "incorrect_analysis_or_route"},
    )
    assert r.status_code == 200
    assert r.json()["changeApproval"]["status"] == "rejected"
    # changeApproval is assembled from mcp_server's OWN approval.py
    # record (unmodified, no identity_id field) -- identity attribution
    # for this decision kind lives only in DecisionFeedback, checked
    # below via the agent-health endpoint, not on changeApproval itself.

    # Health for the architect should now show the rejection with its reason.
    r = client.get("/admin/agents/architect/health", headers=headers(customer="vdb"))
    feedback = r.json()["feedback"]
    rejection = next(f for f in feedback if f["kind"] == "exact_change_rejection")
    assert rejection["reasons"]["incorrect_analysis_or_route"] == 1


def test_domain_owner_edit_maintains_revision_count_deterministically(client, monkeypatch):
    """The reviewer agent's own self-reported revision_count has no
    continuity across a Domain Owner edit cycle (its prompt is never
    told the prior count) -- the router must set it itself, from the
    prior version's count, regardless of what the agent's summary says.
    The fake below deliberately reports an unrelated value (99) to
    prove the router computes it rather than passing the agent's
    number through."""
    story_id = _seed_and_enhance_t001(client, monkeypatch)

    async def _fake_review_with_wrong_count(*, prompt, options):
        summary = {"user_story": {
            "statement": "Revised statement", "business_context": "", "acceptance_criteria": [],
            "test_script": [], "open_questions": [], "quality_status": "passed", "revision_count": 99,
        }}
        yield _result(f"```json\n{json.dumps(summary)}\n```")

    monkeypatch.setattr(sdk, "query", _fake_review_with_wrong_count)

    client.get(f"/changes/{story_id}/domain-review", headers=headers(customer="bwm"))
    client.post(f"/changes/{story_id}/domain-review/start", headers=headers(customer="bwm"), json={"decidedBy": "Ellen Vos"})
    r = client.post(
        f"/changes/{story_id}/domain-review/edit",
        headers=headers(customer="bwm"),
        json={"editedBy": "Ellen Vos", "note": "tightened wording", "userStory": {
            "statement": "Ellen's edit", "businessContext": "", "acceptanceCriteria": [],
            "testScript": [], "openQuestions": [], "qualityStatus": "passed", "revisionCount": 0,
        }},
    )
    assert r.status_code == 200
    history = r.json()["history"]
    assert [v["label"] for v in history] == ["ai_generated", "domain_owner_edit", "reviewer_agent_revision"]
    # ai_generated started at 0 (see test_domain_governance.py's
    # _enhance_summary) -- one edit cycle later this must be 1, not the
    # fake agent's bogus 99.
    assert history[-1]["userStory"]["revisionCount"] == 1
