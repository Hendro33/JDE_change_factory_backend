"""
Tests for Architecture Review history/versioning and "Ask Jade about
this solution" (Increment: Requirement collaboration + Agent
experience, Stage 4). Same deterministic sdk.query monkeypatching
approach as test_requirement_conversation.py.

Covers: a re-analysis (manual retrigger or a recommend_reanalysis
conversation turn followed by one) appends to history rather than
overwriting the Architect's prior reasoning; "Ask Jade about this
solution" only ever explains or recommends a re-run, never applies
anything itself (there is no draft payload for a solution the way
there is for a requirement); and "Ask Jade about this requirement"
remains reachable for the Application Manager on the same change,
alongside the solution conversation, exactly as approved.
"""

from __future__ import annotations

import json

import claude_agent_sdk as sdk

from .conftest import headers
from .test_domain_governance import _result
from .test_requirement_conversation import _to_domain_owner_reviewing


def _architect_summary(route: str = "Functional Agent") -> dict:
    return {
        "architect_decision": {
            "recommended_route": route,
            "confidence": 0.8,
            "existing_functionality_found": "Processing option P42 on version ZJDE0001 already supports a default offset.",
            "alternatives_considered": [{"approach": "Custom UBE", "whyNot": "existing processing option already covers this"}],
            "objects_affected": ["P4210"],
            "dependencies_and_conflicts": [],
            "rollback_strategy": "Reset the processing option to its previous value.",
        },
        "implementation_spec": {
            "sequence": ["Set processing option P42 to 7 working days"],
            "required_mcp_operations": ["propose_change"],
            "human_actions_required": [],
            "validation_approach": "Create a test sales order and confirm the default delivery date.",
        },
    }


async def _fake_architecture_review_success(*, prompt, options):
    yield _result(f"```json\n{json.dumps(_architect_summary())}\n```")


async def _fake_architecture_review_reanalysis(*, prompt, options):
    yield _result(f"```json\n{json.dumps(_architect_summary('Technical Agent'))}\n```")


def _solution_answer(kind: str, answer: str = "Here is why.") -> dict:
    return {"answer": answer, "kind": kind}


async def _fake_solution_explanation(*, prompt, options):
    yield _result(f"```json\n{json.dumps(_solution_answer('explanation', 'The existing processing option already covers this, so no custom build is needed.'))}\n```")


async def _fake_solution_reanalysis(*, prompt, options):
    yield _result(
        f"```json\n{json.dumps(_solution_answer('recommend_reanalysis', 'That changes which objects are affected -- this should be re-analysed.'))}\n```"
    )


async def _fake_solution_error(*, prompt, options):
    yield _result("could not answer", is_error=True)


def _to_application_manager_approved_with_analysis(client, monkeypatch) -> str:
    change_id = _to_domain_owner_reviewing(client, monkeypatch)
    client.post(f"/changes/{change_id}/domain-review/approve", headers=headers(customer="bwm"), json={"decidedBy": "Ellen Vos"})
    monkeypatch.setattr(sdk, "query", _fake_architecture_review_success)
    client.post(
        f"/changes/{change_id}/domain-review/application-manager-approve",
        headers=headers(customer="bwm"), json={"decidedBy": "Hendro"},
    )
    return change_id


# ---------------------------------------------------------------------
# History/versioning -- a re-analysis never overwrites prior reasoning.
# ---------------------------------------------------------------------

def test_architecture_review_completion_is_exposed_and_recorded_in_history(client, monkeypatch):
    change_id = _to_application_manager_approved_with_analysis(client, monkeypatch)

    r = client.get(f"/changes/{change_id}/architecture-review", headers=headers(customer="bwm"))
    assert r.status_code == 200
    body = r.json()
    assert body["stage"] == "done"
    assert len(body["history"]) == 1
    assert body["history"][0]["architectDecision"]["recommendedRoute"] == "Functional Agent"
    # Back-compat top-level fields still present and consistent with the newest history entry.
    assert body["architectDecision"]["recommendedRoute"] == "Functional Agent"
    assert body["conversation"] == []


def test_manual_retrigger_appends_a_new_history_entry_instead_of_overwriting(client, monkeypatch):
    change_id = _to_application_manager_approved_with_analysis(client, monkeypatch)

    monkeypatch.setattr(sdk, "query", _fake_architecture_review_reanalysis)
    r = client.post(f"/changes/{change_id}/architecture-review", headers=headers(customer="bwm"))
    assert r.status_code == 202

    got = client.get(f"/changes/{change_id}/architecture-review", headers=headers(customer="bwm"))
    body = got.json()
    assert len(body["history"]) == 2
    # The first run's reasoning is still there, never discarded.
    assert body["history"][0]["architectDecision"]["recommendedRoute"] == "Functional Agent"
    assert body["history"][1]["architectDecision"]["recommendedRoute"] == "Technical Agent"
    # "Current" fields reflect the newest run.
    assert body["architectDecision"]["recommendedRoute"] == "Technical Agent"


def test_architecture_review_not_yet_available_is_reported_honestly(client, monkeypatch):
    change_id = _to_domain_owner_reviewing(client, monkeypatch)
    r = client.get(f"/changes/{change_id}/architecture-review", headers=headers(customer="bwm"))
    # Not in the Delivery Queue yet -- Gate 1 hasn't cleared.
    assert r.status_code == 409


# ---------------------------------------------------------------------
# "Ask Jade about this solution" -- explain freely, never silently
# apply. A recommend_reanalysis turn is only ever a pointer back at the
# existing manual retrigger, never an automatic re-run.
# ---------------------------------------------------------------------

def test_ask_about_solution_records_an_explanation_without_changing_the_analysis(client, monkeypatch):
    change_id = _to_application_manager_approved_with_analysis(client, monkeypatch)
    monkeypatch.setattr(sdk, "query", _fake_solution_explanation)

    r = client.post(
        f"/changes/{change_id}/architecture-review/ask",
        headers=headers(customer="bwm"),
        json={"askedBy": "Hendro", "question": "Why is this the recommended approach?"},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["conversation"]) == 1
    turn = body["conversation"][0]
    assert turn["kind"] == "explanation"
    assert turn["proposedUserStory"] is None
    assert turn["askedBy"] == "Hendro"
    # The analysis itself is untouched.
    assert len(body["history"]) == 1


def test_ask_about_solution_recommend_reanalysis_never_applies_anything_itself(client, monkeypatch):
    change_id = _to_application_manager_approved_with_analysis(client, monkeypatch)
    monkeypatch.setattr(sdk, "query", _fake_solution_reanalysis)

    r = client.post(
        f"/changes/{change_id}/architecture-review/ask",
        headers=headers(customer="bwm"),
        json={"askedBy": "Hendro", "question": "We also need to update the credit check UBE -- does that change the route?"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["conversation"][0]["kind"] == "recommend_reanalysis"
    # No new history entry from asking alone -- only a real re-run (the
    # existing manual retrigger) ever appends one.
    assert len(body["history"]) == 1
    assert body["architectDecision"]["recommendedRoute"] == "Functional Agent"

    # The human follows the recommendation through the EXISTING manual
    # retrigger -- the conversation turn itself never did this.
    monkeypatch.setattr(sdk, "query", _fake_architecture_review_reanalysis)
    client.post(f"/changes/{change_id}/architecture-review", headers=headers(customer="bwm"))
    got = client.get(f"/changes/{change_id}/architecture-review", headers=headers(customer="bwm"))
    assert len(got.json()["history"]) == 2


def test_ask_about_solution_fails_clearly_when_the_agent_errors(client, monkeypatch):
    change_id = _to_application_manager_approved_with_analysis(client, monkeypatch)
    monkeypatch.setattr(sdk, "query", _fake_solution_error)

    r = client.post(
        f"/changes/{change_id}/architecture-review/ask",
        headers=headers(customer="bwm"),
        json={"askedBy": "Hendro", "question": "Why this route?"},
    )
    assert r.status_code == 502


def test_ask_about_solution_requires_a_question(client, monkeypatch):
    change_id = _to_application_manager_approved_with_analysis(client, monkeypatch)
    r = client.post(
        f"/changes/{change_id}/architecture-review/ask",
        headers=headers(customer="bwm"),
        json={"askedBy": "Hendro", "question": "   "},
    )
    assert r.status_code == 422


def test_ask_about_solution_requires_a_completed_analysis(client, monkeypatch):
    """The real, unmocked sdk.query fails fast in the test environment
    (no live agent runtime), so application-manager-approve's background
    task lands the run in "failed" with no history -- exactly the state
    the endpoint must report honestly rather than pretending there is
    something to discuss."""
    change_id = _to_domain_owner_reviewing(client, monkeypatch)
    client.post(f"/changes/{change_id}/domain-review/approve", headers=headers(customer="bwm"), json={"decidedBy": "Ellen Vos"})
    client.post(
        f"/changes/{change_id}/domain-review/application-manager-approve",
        headers=headers(customer="bwm"), json={"decidedBy": "Hendro"},
    )

    r = client.post(
        f"/changes/{change_id}/architecture-review/ask",
        headers=headers(customer="bwm"),
        json={"askedBy": "Hendro", "question": "Anything?"},
    )
    assert r.status_code == 409


# ---------------------------------------------------------------------
# Both panels the design requires coexist on the same change: "Ask Jade
# about this requirement" (Application Manager, explain-only on an
# already-approved requirement) alongside "Ask Jade about this
# solution" (Architect-backed).
# ---------------------------------------------------------------------

def test_application_manager_can_ask_about_both_the_requirement_and_the_solution(client, monkeypatch):
    change_id = _to_application_manager_approved_with_analysis(client, monkeypatch)

    monkeypatch.setattr(sdk, "query", _fake_solution_explanation)
    solution = client.post(
        f"/changes/{change_id}/architecture-review/ask",
        headers=headers(customer="bwm"),
        json={"askedBy": "Hendro", "question": "Why this route?"},
    )
    assert solution.status_code == 200
    assert solution.json()["conversation"][0]["kind"] == "explanation"

    async def _fake_requirement_explanation(*, prompt, options):
        yield _result(
            '```json\n{"answer": "This applies to every new sales order on this version.", "kind": "explanation", "proposed_user_story": null}\n```'
        )

    monkeypatch.setattr(sdk, "query", _fake_requirement_explanation)
    requirement = client.post(
        f"/changes/{change_id}/domain-review/ask",
        headers=headers(customer="bwm"),
        json={"askedBy": "Hendro", "question": "What business requirement am I authorising delivery for?"},
    )
    assert requirement.status_code == 200
    assert requirement.json()["conversation"][0]["kind"] == "explanation"
    # The two conversations are independent sidecars -- asking about the
    # solution never touched the requirement's own conversation, or vice versa.
    assert len(requirement.json()["conversation"]) == 1
