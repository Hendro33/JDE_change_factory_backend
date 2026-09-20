"""
Tests for business domain ownership and the Domain Owner / Application
Manager governance workflow (Increment: domain-aware governance).

Same mocking approach as test_orchestration.py: claude_agent_sdk.query
is monkeypatched so these are deterministic and spend no real API
calls, while everything downstream (the DomainReview sidecar, the real
backlog.py record, customer scoping) is exercised for real.
"""

from __future__ import annotations

import json

import claude_agent_sdk as sdk
import pytest
from jde_mcp_server import backlog

from .conftest import headers


def _sys(subagent_type: str) -> sdk.SystemMessage:
    return sdk.SystemMessage(subtype="task_started", data={"subagent_type": subagent_type})


def _result(text: str, is_error: bool = False) -> sdk.ResultMessage:
    return sdk.ResultMessage(
        subtype="success" if not is_error else "error",
        duration_ms=1, duration_api_ms=1, is_error=is_error, num_turns=1,
        session_id="test-session", result=text,
    )


def _enhance_summary(story_id: str) -> dict:
    return {
        "story_id": story_id,
        "user_story": {
            "statement": "As a sales team member, I want the requested delivery date to default to 7 working days from the order date, so that I stop manually correcting it.",
            "business_context": "Sales order entry currently defaults delivery date to today.",
            "acceptance_criteria": [{"id": "AC1", "text": "Defaults to order date + 7 working days.", "verified_by": "T1"}],
            "test_script": [{"id": "T1", "action": "Create a new sales order", "expected": "Delivery date defaults correctly"}],
            "open_questions": ["Should public holidays be excluded?"],
            "quality_status": "passed",
            "revision_count": 0,
        },
        "business_impact": {"financial_impact": "", "operational_reach": "Sales team", "risk_compliance": "", "strategic_alignment": "", "urgency": ""},
        "rough_complexity_signal": "Medium",
        "check_outcome": "proposed_to_backlog",
        "failed_criteria": [],
    }


async def _fake_enhance_success(*, prompt, options):
    import re

    m = re.search(r"story_id to use throughout, in every tool call: (\S+)", prompt)
    story_id = m.group(1)
    summary = _enhance_summary(story_id)
    yield _sys("receive-agent")
    yield _sys("improve-agent")
    yield _sys("check-agent")
    backlog.propose_to_backlog(
        story_id, summary["user_story"]["statement"], summary["business_impact"],
        summary["rough_complexity_signal"], source="Support / Topdesk, Topdesk T001",
    )
    yield _result(f"```json\n{json.dumps(summary)}\n```")


def _seed_and_enhance_t001(client, monkeypatch) -> str:
    """Gets a change all the way to BACKLOG_READY with a real user
    story -- the precondition every domain-governance endpoint needs,
    exactly mirroring how T001 sits in the real pilot data."""
    monkeypatch.setattr(sdk, "query", _fake_enhance_success)
    r = client.post(
        "/change-requests",
        headers=headers(customer="bwm"),
        json={
            "title": "Default delivery date is wrong",
            "businessSource": "Support / Topdesk",
            "sourceReference": "Topdesk T001",
            "rawContent": "When our sales team enters a new sales order...",
        },
    )
    request_id = r.json()["id"]
    client.post(f"/changes/{request_id}/enhance", headers=headers(customer="bwm"))
    return request_id


def _reviewer_agent_summary() -> dict:
    return {
        "user_story": {
            "statement": "As a sales team member, I want the requested delivery date to default to 7 WORKING days (excluding public holidays) from the order date, so that I stop manually correcting it.",
            "business_context": "Sales order entry currently defaults delivery date to today; the Domain Owner clarified that public holidays should also be excluded.",
            "acceptance_criteria": [{"id": "AC1", "text": "Defaults to order date + 7 working days, excluding weekends and public holidays.", "verified_by": "T1"}],
            "test_script": [{"id": "T1", "action": "Create a new sales order", "expected": "Delivery date defaults correctly, holidays excluded"}],
            "open_questions": [],
            "quality_status": "passed",
            "revision_count": 1,
        }
    }


async def _fake_review_success(*, prompt, options):
    yield _result(f"```json\n{json.dumps(_reviewer_agent_summary())}\n```")


async def _fake_review_error(*, prompt, options):
    yield _result("the reviewer agent choked", is_error=True)


# ---------------------------------------------------------------------
# Business domain listing + customer isolation
# ---------------------------------------------------------------------

def test_business_domains_are_seeded_for_bicycleworks(client):
    r = client.get("/business-domains", headers=headers(customer="bwm"))
    assert r.status_code == 200
    domains = r.json()
    assert len(domains) == 5
    assert {d["apqcCode"] for d in domains} == {"4.1", "4.4", "4.4.3", "6.1", "9.3"}


def test_business_domains_are_isolated_per_customer(client):
    r = client.get("/business-domains", headers=headers(customer="vdb"))
    assert r.status_code == 200
    assert r.json() == []  # vdb has no domains -- bwm's are invisible here


def test_domain_from_another_customer_cannot_be_assigned(client, monkeypatch):
    change_id = _seed_and_enhance_t001(client, monkeypatch)
    r = client.post(
        f"/changes/{change_id}/domain-review/assign-domain",
        headers=headers(customer="bwm"),
        json={"businessDomainId": "DOM-DOES-NOT-EXIST"},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------
# Domain assignment, incl. exposing classification uncertainty
# ---------------------------------------------------------------------

def test_assign_domain_to_a_change(client, monkeypatch):
    change_id = _seed_and_enhance_t001(client, monkeypatch)
    r = client.post(
        f"/changes/{change_id}/domain-review/assign-domain",
        headers=headers(customer="bwm"),
        json={"businessDomainId": "DOM-BWM-ORDER-FULFIL"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["businessDomainId"] == "DOM-BWM-ORDER-FULFIL"
    assert body["domainClassificationUncertain"] is False

    # And it's reflected on the Change itself, for list/filter display.
    r2 = client.get(f"/changes/{change_id}", headers=headers(customer="bwm"))
    assert r2.json()["businessDomainId"] == "DOM-BWM-ORDER-FULFIL"


def test_uncertain_classification_is_exposed_not_invented(client, monkeypatch):
    change_id = _seed_and_enhance_t001(client, monkeypatch)
    r = client.post(
        f"/changes/{change_id}/domain-review/assign-domain",
        headers=headers(customer="bwm"),
        json={"uncertain": True, "note": "Too vague to place in an existing domain."},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["businessDomainId"] is None
    assert body["domainClassificationUncertain"] is True
    assert body["domainClassificationNote"] == "Too vague to place in an existing domain."


def test_domain_review_requires_a_user_story_first(client):
    r = client.get("/changes/CR-BW-T003/domain-review", headers=headers(customer="bwm"))
    assert r.status_code == 404


# ---------------------------------------------------------------------
# Domain Owner review lifecycle + stage-gating
# ---------------------------------------------------------------------

def test_domain_review_starts_ready_for_domain_owner_with_ai_version_preserved(client, monkeypatch):
    change_id = _seed_and_enhance_t001(client, monkeypatch)
    r = client.get(f"/changes/{change_id}/domain-review", headers=headers(customer="bwm"))
    assert r.status_code == 200
    body = r.json()
    assert body["stage"] == "ready_for_domain_owner"
    assert len(body["history"]) == 1
    assert body["history"][0]["label"] == "ai_generated"


def test_cannot_edit_before_starting_review(client, monkeypatch):
    change_id = _seed_and_enhance_t001(client, monkeypatch)
    client.get(f"/changes/{change_id}/domain-review", headers=headers(customer="bwm"))
    r = client.post(
        f"/changes/{change_id}/domain-review/edit",
        headers=headers(customer="bwm"),
        json={"editedBy": "Domain Owner", "userStory": _enhance_summary(change_id)["user_story"]},
    )
    assert r.status_code == 409


def test_cannot_approve_before_reviewing(client, monkeypatch):
    change_id = _seed_and_enhance_t001(client, monkeypatch)
    client.get(f"/changes/{change_id}/domain-review", headers=headers(customer="bwm"))
    r = client.post(
        f"/changes/{change_id}/domain-review/approve",
        headers=headers(customer="bwm"),
        json={"decidedBy": "Domain Owner"},
    )
    assert r.status_code == 409


def test_start_review_transitions_and_is_idempotent(client, monkeypatch):
    change_id = _seed_and_enhance_t001(client, monkeypatch)
    client.get(f"/changes/{change_id}/domain-review", headers=headers(customer="bwm"))
    r1 = client.post(f"/changes/{change_id}/domain-review/start", headers=headers(customer="bwm"), json={"decidedBy": "Domain Owner"})
    assert r1.json()["stage"] == "domain_owner_reviewing"
    r2 = client.post(f"/changes/{change_id}/domain-review/start", headers=headers(customer="bwm"), json={"decidedBy": "Domain Owner"})
    assert r2.status_code == 200  # idempotent, not an error


# ---------------------------------------------------------------------
# Domain Owner edit -> Reviewer Agent -> revised version, never a
# silent auto-approval, both versions preserved as evidence.
# ---------------------------------------------------------------------

def test_edit_is_routed_through_reviewer_agent_and_both_versions_preserved(client, monkeypatch):
    change_id = _seed_and_enhance_t001(client, monkeypatch)
    client.get(f"/changes/{change_id}/domain-review", headers=headers(customer="bwm"))
    client.post(f"/changes/{change_id}/domain-review/start", headers=headers(customer="bwm"), json={"decidedBy": "Domain Owner"})

    monkeypatch.setattr(sdk, "query", _fake_review_success)
    edited_story = _enhance_summary(change_id)["user_story"]
    edited_story["statement"] = "As a sales team member, I want delivery dates to also exclude public holidays..."

    r = client.post(
        f"/changes/{change_id}/domain-review/edit",
        headers=headers(customer="bwm"),
        json={"editedBy": "Domain Owner", "note": "Please also exclude public holidays.", "userStory": edited_story},
    )
    assert r.status_code == 200
    body = r.json()

    # Back to domain_owner_reviewing -- never auto-approved.
    assert body["stage"] == "domain_owner_reviewing"

    labels = [v["label"] for v in body["history"]]
    assert labels == ["ai_generated", "domain_owner_edit", "reviewer_agent_revision"]

    # The original AI statement is untouched in history entry 1.
    assert "7 working days from the order date" in body["history"][0]["userStory"]["statement"]
    # The Domain Owner's own edit is preserved verbatim in entry 2.
    assert body["history"][1]["userStory"]["statement"] == edited_story["statement"]
    assert body["history"][1]["actor"] == "Domain Owner"
    # The Reviewer Agent's revision (entry 3) is what the fake produced,
    # not a copy of the Domain Owner's raw edit.
    assert "excluding weekends and public holidays" in body["history"][2]["userStory"]["acceptanceCriteria"][0]["text"]
    assert body["history"][2]["actor"] == "Reviewer Agent"


def test_reviewer_agent_failure_does_not_lose_the_domain_owner_edit(client, monkeypatch):
    change_id = _seed_and_enhance_t001(client, monkeypatch)
    client.get(f"/changes/{change_id}/domain-review", headers=headers(customer="bwm"))
    client.post(f"/changes/{change_id}/domain-review/start", headers=headers(customer="bwm"), json={"decidedBy": "Domain Owner"})

    monkeypatch.setattr(sdk, "query", _fake_review_error)
    edited_story = _enhance_summary(change_id)["user_story"]

    r = client.post(
        f"/changes/{change_id}/domain-review/edit",
        headers=headers(customer="bwm"),
        json={"editedBy": "Domain Owner", "userStory": edited_story},
    )
    assert r.status_code == 502

    # The edit itself is safe -- recorded, and the stage reflects that
    # a revision was requested but not yet refined, not lost data.
    r2 = client.get(f"/changes/{change_id}/domain-review", headers=headers(customer="bwm"))
    body = r2.json()
    assert body["stage"] == "domain_owner_requested_revision"
    assert [v["label"] for v in body["history"]] == ["ai_generated", "domain_owner_edit"]


# ---------------------------------------------------------------------
# Domain Owner approval vs Application Manager approval -- two
# separate decisions, neither implying the other.
# ---------------------------------------------------------------------

def test_domain_owner_approval_does_not_clear_gate_2(client, monkeypatch):
    change_id = _seed_and_enhance_t001(client, monkeypatch)
    client.get(f"/changes/{change_id}/domain-review", headers=headers(customer="bwm"))
    client.post(f"/changes/{change_id}/domain-review/start", headers=headers(customer="bwm"), json={"decidedBy": "Domain Owner"})

    r = client.post(
        f"/changes/{change_id}/domain-review/approve",
        headers=headers(customer="bwm"),
        json={"decidedBy": "Ellen Vos", "note": "Business requirement looks right."},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["stage"] == "ready_for_application_manager"
    assert body["domainOwnerApproval"]["kind"] == "domain_owner"
    assert body["domainOwnerApproval"]["approvedBy"] == "Ellen Vos"
    assert body["applicationManagerApproval"] is None

    # Gate 2 (backlog.py) is UNCHANGED -- still not approved. Phase 3
    # tools must still refuse this story.
    with pytest.raises(backlog.StoryNotApproved):
        backlog.require_approved(change_id)

    # And the Change's own state hasn't jumped to APPROVED either.
    r2 = client.get(f"/changes/{change_id}", headers=headers(customer="bwm"))
    assert r2.json()["state"] == "BACKLOG_READY"


def test_application_manager_approval_is_a_separate_step_that_clears_gate_2(client, monkeypatch):
    change_id = _seed_and_enhance_t001(client, monkeypatch)
    client.get(f"/changes/{change_id}/domain-review", headers=headers(customer="bwm"))
    client.post(f"/changes/{change_id}/domain-review/start", headers=headers(customer="bwm"), json={"decidedBy": "Domain Owner"})
    client.post(f"/changes/{change_id}/domain-review/approve", headers=headers(customer="bwm"), json={"decidedBy": "Ellen Vos"})

    # Cannot skip straight to Application Manager approval without the
    # Domain Owner step having happened -- already proven by getting
    # here via the approve call above; now prove the reverse ordering
    # is enforced too, on a fresh story with no domain owner approval.
    other_change_id = _seed_and_enhance_t001(client, monkeypatch)
    client.get(f"/changes/{other_change_id}/domain-review", headers=headers(customer="bwm"))
    r_blocked = client.post(
        f"/changes/{other_change_id}/domain-review/application-manager-approve",
        headers=headers(customer="bwm"),
        json={"decidedBy": "Hendro"},
    )
    assert r_blocked.status_code == 409

    r = client.post(
        f"/changes/{change_id}/domain-review/application-manager-approve",
        headers=headers(customer="bwm"),
        json={"decidedBy": "Hendro", "note": "Slotting into next sprint."},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["stage"] == "application_manager_approved"
    assert body["applicationManagerApproval"]["kind"] == "application_manager"
    assert body["applicationManagerApproval"]["approvedBy"] == "Hendro"

    # NOW Gate 2 is actually cleared -- via the real, unmodified
    # backlog.approve() function.
    approved_record = backlog.require_approved(change_id)
    assert approved_record["status"] == "approved"
    assert approved_record["decided_by"] == "Hendro"

    r2 = client.get(f"/changes/{change_id}", headers=headers(customer="bwm"))
    assert r2.json()["state"] == "APPROVED"


# ---------------------------------------------------------------------
# Rejection -- terminal, distinct from a revision request, requires a
# reason. Domain Owner rejection stays in the sidecar; Application
# Manager rejection is the one that also reaches the real backlog.py.
# ---------------------------------------------------------------------

def test_cannot_reject_before_reviewing(client, monkeypatch):
    change_id = _seed_and_enhance_t001(client, monkeypatch)
    client.get(f"/changes/{change_id}/domain-review", headers=headers(customer="bwm"))
    r = client.post(
        f"/changes/{change_id}/domain-review/reject",
        headers=headers(customer="bwm"),
        json={"decidedBy": "Domain Owner", "note": "Not a real requirement."},
    )
    assert r.status_code == 409


def test_domain_owner_reject_requires_a_reason(client, monkeypatch):
    change_id = _seed_and_enhance_t001(client, monkeypatch)
    client.get(f"/changes/{change_id}/domain-review", headers=headers(customer="bwm"))
    client.post(f"/changes/{change_id}/domain-review/start", headers=headers(customer="bwm"), json={"decidedBy": "Domain Owner"})
    r = client.post(
        f"/changes/{change_id}/domain-review/reject",
        headers=headers(customer="bwm"),
        json={"decidedBy": "Ellen Vos"},
    )
    assert r.status_code == 422


def test_domain_owner_reject_is_terminal_and_never_touches_backlog(client, monkeypatch):
    change_id = _seed_and_enhance_t001(client, monkeypatch)
    client.get(f"/changes/{change_id}/domain-review", headers=headers(customer="bwm"))
    client.post(f"/changes/{change_id}/domain-review/start", headers=headers(customer="bwm"), json={"decidedBy": "Domain Owner"})

    r = client.post(
        f"/changes/{change_id}/domain-review/reject",
        headers=headers(customer="bwm"),
        json={"decidedBy": "Ellen Vos", "note": "This need no longer exists.", "rejectionReason": "duplicate_or_superseded"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["stage"] == "domain_owner_rejected"
    assert body["domainOwnerApproval"]["kind"] == "domain_owner"
    assert body["domainOwnerApproval"]["status"] == "rejected"
    assert body["domainOwnerApproval"]["approvedBy"] == "Ellen Vos"
    assert body["applicationManagerApproval"] is None

    # Never reached mcp_server -- Gate 2 has no record of this at all,
    # and the Change's own state is untouched by a Domain Owner
    # decision, same as an approval never jumping it to APPROVED.
    with pytest.raises(backlog.StoryNotApproved):
        backlog.require_approved(change_id)
    r2 = client.get(f"/changes/{change_id}", headers=headers(customer="bwm"))
    assert r2.json()["state"] == "BACKLOG_READY"
    assert r2.json()["domainReviewStage"] == "domain_owner_rejected"

    # Terminal: cannot then approve or edit from a rejected stage.
    r3 = client.post(
        f"/changes/{change_id}/domain-review/approve",
        headers=headers(customer="bwm"),
        json={"decidedBy": "Ellen Vos"},
    )
    assert r3.status_code == 409

    # And it drops out of User Story Review's own queue definition
    # (mirrors PRE_DOMAIN_OWNER_APPROVAL_STAGES in metrics_service.py).
    r4 = client.get("/metrics", headers=headers(customer="bwm"))
    # Nothing else in this fixture reaches BACKLOG_READY, so the
    # "awaiting domain owner" count is back to zero once this one is
    # no longer pending.
    awaiting = next(t for t in r4.json()["totals"] if t["key"] == "awaiting_domain_owner")
    assert awaiting["value"] == 0


def test_domain_owner_rejection_is_captured_as_decision_feedback(client, monkeypatch):
    change_id = _seed_and_enhance_t001(client, monkeypatch)
    client.get(f"/changes/{change_id}/domain-review", headers=headers(customer="bwm"))
    client.post(f"/changes/{change_id}/domain-review/start", headers=headers(customer="bwm"), json={"decidedBy": "Domain Owner"})
    client.post(
        f"/changes/{change_id}/domain-review/reject",
        headers=headers(customer="bwm"),
        json={"decidedBy": "Ellen Vos", "note": "Duplicate of another request.", "rejectionReason": "duplicate_or_superseded"},
    )
    r = client.get("/admin/agents/improve-agent/health", headers=headers(customer="bwm"))
    assert r.status_code == 200
    feedback = {f["kind"]: f for f in r.json()["feedback"]}
    assert feedback["domain_owner_rejection"]["count"] == 1
    assert feedback["domain_owner_rejection"]["reasons"]["duplicate_or_superseded"] == 1


def test_application_manager_reject_requires_a_reason(client, monkeypatch):
    change_id = _seed_and_enhance_t001(client, monkeypatch)
    client.get(f"/changes/{change_id}/domain-review", headers=headers(customer="bwm"))
    client.post(f"/changes/{change_id}/domain-review/start", headers=headers(customer="bwm"), json={"decidedBy": "Domain Owner"})
    client.post(f"/changes/{change_id}/domain-review/approve", headers=headers(customer="bwm"), json={"decidedBy": "Ellen Vos"})
    r = client.post(
        f"/changes/{change_id}/domain-review/application-manager-reject",
        headers=headers(customer="bwm"),
        json={"decidedBy": "Hendro"},
    )
    assert r.status_code == 422


def test_application_manager_reject_actually_rejects_gate_2_via_backlog(client, monkeypatch):
    change_id = _seed_and_enhance_t001(client, monkeypatch)
    client.get(f"/changes/{change_id}/domain-review", headers=headers(customer="bwm"))
    client.post(f"/changes/{change_id}/domain-review/start", headers=headers(customer="bwm"), json={"decidedBy": "Domain Owner"})
    client.post(f"/changes/{change_id}/domain-review/approve", headers=headers(customer="bwm"), json={"decidedBy": "Ellen Vos"})

    r = client.post(
        f"/changes/{change_id}/domain-review/application-manager-reject",
        headers=headers(customer="bwm"),
        json={"decidedBy": "Hendro", "note": "Conflicts with an in-flight upgrade.", "rejectionReason": "risk_or_compliance_concern"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["stage"] == "application_manager_rejected"
    assert body["applicationManagerApproval"]["status"] == "rejected"
    assert body["applicationManagerApproval"]["approvedBy"] == "Hendro"

    # Gate 2 (backlog.py, unmodified) genuinely reflects the rejection.
    with pytest.raises(backlog.StoryNotApproved):
        backlog.require_approved(change_id)

    r2 = client.get(f"/changes/{change_id}", headers=headers(customer="bwm"))
    assert r2.json()["state"] == "REJECTED"

    # No Delivery Queue entry was ever created for a rejected story.
    r3 = client.get("/delivery-queue", headers=headers(customer="bwm"))
    assert all(e["changeId"] != change_id for e in r3.json())

    # Terminal: cannot then approve for delivery either.
    r4 = client.post(
        f"/changes/{change_id}/domain-review/application-manager-approve",
        headers=headers(customer="bwm"),
        json={"decidedBy": "Hendro"},
    )
    assert r4.status_code == 409


def test_domain_governance_is_customer_scoped(client, monkeypatch):
    change_id = _seed_and_enhance_t001(client, monkeypatch)
    r = client.get(f"/changes/{change_id}/domain-review", headers=headers(user="u-hendro", customer="vdb"))
    assert r.status_code == 404  # exists for bwm, invisible to vdb


# ---------------------------------------------------------------------
# Dashboard domain metrics -- derived from real records, never hard-coded
# ---------------------------------------------------------------------

def test_metrics_have_no_domain_breakdown_before_any_story_reaches_backlog(client):
    r = client.get("/metrics", headers=headers(customer="bwm"))
    assert r.status_code == 200
    assert r.json()["businessDomainBreakdown"] == []


def test_metrics_bucket_unclassified_and_classified_domains_separately(client, monkeypatch):
    change_id = _seed_and_enhance_t001(client, monkeypatch)
    # Unclassified until assigned.
    r = client.get("/metrics", headers=headers(customer="bwm"))
    breakdown = r.json()["businessDomainBreakdown"]
    assert breakdown == [{"domainId": None, "domainName": "Unclassified / needs review", "apqcCode": "", "count": 1}]

    client.post(
        f"/changes/{change_id}/domain-review/assign-domain",
        headers=headers(customer="bwm"),
        json={"businessDomainId": "DOM-BWM-ORDER-FULFIL"},
    )
    r2 = client.get("/metrics", headers=headers(customer="bwm"))
    breakdown2 = r2.json()["businessDomainBreakdown"]
    assert breakdown2 == [
        {"domainId": "DOM-BWM-ORDER-FULFIL", "domainName": "Order Fulfilment & Shipment Management", "apqcCode": "4.4.3", "count": 1}
    ]
