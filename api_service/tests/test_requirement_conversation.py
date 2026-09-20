"""
Tests for "Ask Jade about this requirement" -- requirement
collaboration (Increment: Requirement collaboration + Agent
experience). Same deterministic sdk.query monkeypatching approach as
test_domain_governance.py.

Covers the design's core safety property throughout: explanation never
touches the requirement; a proposed amendment is recorded for review
but never auto-applied; applying one still goes through the EXISTING
/domain-review/edit endpoint (Improve + versioning, unchanged); and an
Application Manager on an already-approved requirement can surface a
concern but never silently amend it -- only explicitly reopen Domain
Owner review via request-reconsideration.
"""

from __future__ import annotations

import json

import claude_agent_sdk as sdk

from .conftest import headers
from .test_domain_governance import _fake_enhance_success, _fake_review_success, _result, _seed_and_enhance_t001


def _ask_answer(kind: str, proposed: dict | None = None, answer: str = "Here is the answer.") -> dict:
    return {"answer": answer, "kind": kind, "proposed_user_story": proposed}


async def _fake_ask_explanation(*, prompt, options):
    yield _result(f"```json\n{json.dumps(_ask_answer('explanation', answer='The default applies to every new sales order on this version.'))}\n```")


def _amended_story() -> dict:
    return {
        "statement": "As a sales team member, I want the requested delivery date to default to 7 working days from the order date, excluding weekends, so that I stop manually correcting it.",
        "business_context": "Sales order entry currently defaults delivery date to today; the Domain Owner clarified weekends must be excluded too.",
        "acceptance_criteria": [{"id": "AC1", "text": "Defaults to order date + 7 working days, weekends excluded.", "verified_by": "T1"}],
        "test_script": [{"id": "T1", "action": "Create a new sales order", "expected": "Delivery date defaults correctly, weekends excluded"}],
        "business_rules": ["Weekends are never counted toward the 7 working days."],
        "assumptions": [],
        "open_questions": [],
        "quality_status": "passed",
        "revision_count": 0,
    }


async def _fake_ask_amendment(*, prompt, options):
    yield _result(
        f"```json\n{json.dumps(_ask_answer('proposed_amendment', proposed=_amended_story(), answer='That is new information -- I would exclude weekends too.'))}\n```"
    )


async def _fake_ask_error(*, prompt, options):
    yield _result("could not answer", is_error=True)


def _to_domain_owner_reviewing(client, monkeypatch) -> str:
    change_id = _seed_and_enhance_t001(client, monkeypatch)
    client.post(f"/changes/{change_id}/domain-review/start", headers=headers(customer="bwm"), json={"decidedBy": "Ellen Vos"})
    return change_id


def _to_application_manager_approved(client, monkeypatch) -> str:
    change_id = _to_domain_owner_reviewing(client, monkeypatch)
    client.post(f"/changes/{change_id}/domain-review/approve", headers=headers(customer="bwm"), json={"decidedBy": "Ellen Vos"})
    client.post(
        f"/changes/{change_id}/domain-review/application-manager-approve",
        headers=headers(customer="bwm"), json={"decidedBy": "Hendro"},
    )
    return change_id


# ---------------------------------------------------------------------
# Explanation vs. proposed amendment
# ---------------------------------------------------------------------

def test_ask_records_an_explanation_without_changing_the_requirement(client, monkeypatch):
    change_id = _to_domain_owner_reviewing(client, monkeypatch)
    monkeypatch.setattr(sdk, "query", _fake_ask_explanation)

    r = client.post(
        f"/changes/{change_id}/domain-review/ask",
        headers=headers(customer="bwm"),
        json={"askedBy": "Ellen Vos", "question": "Does this apply to every sales order or just some?"},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["conversation"]) == 1
    turn = body["conversation"][0]
    assert turn["kind"] == "explanation"
    assert turn["proposedUserStory"] is None
    assert turn["askedBy"] == "Ellen Vos"
    # The requirement itself is untouched -- still just the original AI-generated version.
    assert len(body["history"]) == 1


def test_ask_records_a_proposed_amendment_but_never_applies_it(client, monkeypatch):
    change_id = _to_domain_owner_reviewing(client, monkeypatch)
    monkeypatch.setattr(sdk, "query", _fake_ask_amendment)

    r = client.post(
        f"/changes/{change_id}/domain-review/ask",
        headers=headers(customer="bwm"),
        json={"askedBy": "Ellen Vos", "question": "Weekends should not count towards the 7 days."},
    )
    assert r.status_code == 200
    body = r.json()
    turn = body["conversation"][0]
    assert turn["kind"] == "proposed_amendment"
    assert turn["proposedUserStory"]["businessRules"] == ["Weekends are never counted toward the 7 working days."]
    # Still not applied -- history is exactly as it was before asking.
    assert len(body["history"]) == 1
    assert body["history"][0]["userStory"]["businessRules"] == []


def test_ask_fails_clearly_when_the_agent_errors(client, monkeypatch):
    change_id = _to_domain_owner_reviewing(client, monkeypatch)
    monkeypatch.setattr(sdk, "query", _fake_ask_error)

    r = client.post(
        f"/changes/{change_id}/domain-review/ask",
        headers=headers(customer="bwm"),
        json={"askedBy": "Ellen Vos", "question": "Why this route?"},
    )
    assert r.status_code == 502


def test_ask_requires_a_question(client, monkeypatch):
    change_id = _to_domain_owner_reviewing(client, monkeypatch)
    r = client.post(
        f"/changes/{change_id}/domain-review/ask",
        headers=headers(customer="bwm"),
        json={"askedBy": "Ellen Vos", "question": "   "},
    )
    assert r.status_code == 422


def test_ask_requires_a_requirement_to_exist(client, monkeypatch):
    change_id = _seed_and_enhance_t001(client, monkeypatch)
    # No GET /domain-review yet -- ensure() never ran, so no DomainReview record exists.
    r = client.post(
        f"/changes/{change_id}/domain-review/ask",
        headers=headers(customer="bwm"),
        json={"askedBy": "Ellen Vos", "question": "Anything?"},
    )
    assert r.status_code == 409


# ---------------------------------------------------------------------
# Accepting a proposed amendment reuses the EXISTING edit endpoint --
# no new versioning mechanism.
# ---------------------------------------------------------------------

def test_accepting_a_proposed_amendment_goes_through_the_existing_governed_edit_flow(client, monkeypatch):
    change_id = _to_domain_owner_reviewing(client, monkeypatch)
    monkeypatch.setattr(sdk, "query", _fake_ask_amendment)
    ask = client.post(
        f"/changes/{change_id}/domain-review/ask",
        headers=headers(customer="bwm"),
        json={"askedBy": "Ellen Vos", "question": "Weekends should not count."},
    )
    proposed = ask.json()["conversation"][0]["proposedUserStory"]

    # The Domain Owner reviews Jade's draft and explicitly submits it --
    # the SAME endpoint a manual edit already uses, so it gets the SAME
    # treatment: versioned, and routed through a real Improve pass.
    monkeypatch.setattr(sdk, "query", _fake_review_success)
    r = client.post(
        f"/changes/{change_id}/domain-review/edit",
        headers=headers(customer="bwm"),
        json={"editedBy": "Ellen Vos", "note": "Accepted from Ask Jade about this requirement.", "userStory": proposed},
    )
    assert r.status_code == 200
    body = r.json()
    # ai_generated -> domain_owner_edit -> reviewer_agent_revision.
    assert len(body["history"]) == 3
    assert body["history"][1]["label"] == "domain_owner_edit"
    assert body["history"][1]["note"] == "Accepted from Ask Jade about this requirement."
    assert body["history"][2]["label"] == "reviewer_agent_revision"
    assert body["stage"] == "domain_owner_reviewing"


# ---------------------------------------------------------------------
# Application Manager, on an already-approved requirement: explain
# freely, but never silently amend.
# ---------------------------------------------------------------------

def test_application_manager_can_ask_about_an_already_approved_requirement(client, monkeypatch):
    change_id = _to_application_manager_approved(client, monkeypatch)
    monkeypatch.setattr(sdk, "query", _fake_ask_explanation)

    r = client.post(
        f"/changes/{change_id}/domain-review/ask",
        headers=headers(customer="bwm"),
        json={"askedBy": "Hendro", "question": "What business requirement am I authorising delivery for?"},
    )
    assert r.status_code == 200
    assert r.json()["conversation"][0]["kind"] == "explanation"
    assert r.json()["stage"] == "application_manager_approved"


def test_proposed_amendment_on_an_approved_requirement_cannot_be_applied_directly(client, monkeypatch):
    change_id = _to_application_manager_approved(client, monkeypatch)
    monkeypatch.setattr(sdk, "query", _fake_ask_amendment)
    ask = client.post(
        f"/changes/{change_id}/domain-review/ask",
        headers=headers(customer="bwm"),
        json={"askedBy": "Hendro", "question": "Should weekends count?"},
    )
    proposed = ask.json()["conversation"][0]["proposedUserStory"]
    assert proposed is not None

    # The existing edit endpoint is the only way to apply a story
    # change, and it already refuses outside domain_owner_reviewing --
    # this is what actually prevents a silent amendment here, not a
    # special case in /ask.
    r = client.post(
        f"/changes/{change_id}/domain-review/edit",
        headers=headers(customer="bwm"),
        json={"editedBy": "Hendro", "note": "", "userStory": proposed},
    )
    assert r.status_code == 409


def test_request_reconsideration_requires_a_reconsiderable_stage(client, monkeypatch):
    change_id = _to_domain_owner_reviewing(client, monkeypatch)
    r = client.post(
        f"/changes/{change_id}/domain-review/request-reconsideration",
        headers=headers(customer="bwm"),
        json={"decidedBy": "Hendro", "note": "Something looks off."},
    )
    assert r.status_code == 409


def test_request_reconsideration_requires_a_note(client, monkeypatch):
    change_id = _to_application_manager_approved(client, monkeypatch)
    r = client.post(
        f"/changes/{change_id}/domain-review/request-reconsideration",
        headers=headers(customer="bwm"),
        json={"decidedBy": "Hendro"},
    )
    assert r.status_code == 422


def test_request_reconsideration_reopens_domain_owner_review(client, monkeypatch):
    change_id = _to_application_manager_approved(client, monkeypatch)

    r = client.post(
        f"/changes/{change_id}/domain-review/request-reconsideration",
        headers=headers(customer="bwm"),
        json={"decidedBy": "Hendro", "note": "The requester now says weekends should not count -- please re-check with the Domain Owner."},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["stage"] == "domain_owner_reviewing"

    # This is the existing, governed flow now -- the Domain Owner can
    # act on it exactly as any other domain_owner_reviewing story.
    r2 = client.post(
        f"/changes/{change_id}/domain-review/approve",
        headers=headers(customer="bwm"),
        json={"decidedBy": "Ellen Vos"},
    )
    assert r2.status_code == 200

    # Reconsideration never touches mcp_server -- Gate 2 stays cleared,
    # an honest, documented limitation (see request_reconsideration's
    # own docstring), not silently papered over.
    from jde_mcp_server import backlog
    assert backlog.require_approved(change_id)["status"] == "approved"


def test_agent_health_surfaces_requirement_reconsideration_feedback(client, monkeypatch):
    change_id = _to_application_manager_approved(client, monkeypatch)
    client.post(
        f"/changes/{change_id}/domain-review/request-reconsideration",
        headers=headers(customer="bwm"),
        json={"decidedBy": "Hendro", "note": "Needs a second look."},
    )
    # Not force-fit onto any agent's health card -- same "pure human
    # governance step" reasoning as application_manager_approval/
    # rejection already get in admin.py's _AGENT_FEEDBACK_KINDS.
    r = client.get("/admin/agents/improve-agent/health", headers=headers(customer="bwm"))
    kinds = {f["kind"] for f in r.json()["feedback"]}
    assert "requirement_reconsideration_requested" not in kinds
