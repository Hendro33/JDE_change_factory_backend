"""Repair brief 24 Oct, priority 1: who owns an open point, and when a design is ready.

Technical investigation is JADE's work whatever the wording; people get only real decisions,
missing information and interventions. Unknown facts keep blocking authorisation, and every
approval surface (next action, buttons, endpoints) uses the same readiness rule."""

from __future__ import annotations

import pytest

from jde_api_service.models.issues import AgentResult, ResultIssue
from jde_api_service.services import readiness
from jde_api_service.services.design_quality import classify_story_question, is_technical_investigation

from .test_agent_checks import _derive
from .test_owned_issues import facts  # noqa: F401 -- fixture

TECHNICAL = [
    "Which source JDE object, table or report currently holds the forward contract balance? The Architect should resolve this.",
    "Where is the remaining contract quantity stored in JD Edwards?",
    "What test data (customer and item) can be used in DEV for this?",
    "Does the customer master already have a field for the hedge flag?",
    "Is there an existing report that lists open forward contracts?",
    "Which version of R42520 prints the pick slip today?",
    "The Architect needs to confirm which UDC table holds the shelf-life codes.",
    "What value does processing option 3 of P4210 currently hold?",
]
BUSINESS = [
    "Is the HS code the same for all destinations, or country specific?",
    "Which GL account should freight revenue post to?",
    "Should credit orders also default to order type SO?",
    "Who approves a credit limit override?",
    "Which customers should get the freight surcharge?",
    "How many days of remaining shelf life does a customer require at dispatch?",
]


@pytest.mark.parametrize("q", TECHNICAL)
def test_technical_investigation_is_jades_whatever_the_wording(q):
    assert classify_story_question(q) == "jade", q


@pytest.mark.parametrize("q", BUSINESS)
def test_business_choices_stay_with_people(q):
    assert classify_story_question(q) == "business", q


def test_a_decision_about_unknown_technical_values_without_alternatives_is_investigation():
    assert is_technical_investigation("Decide the processing option values for the new sales order version",
                                      kind="decision", options=[], text="The target values are unknown.")
    assert not is_technical_investigation("Reuse line type F or create a new freight line type?", kind="decision",
                                          options=["Reuse F (no set-up)", "New line type FR (separate reporting)"])


def test_technical_questions_leave_the_business_queue_but_keep_blocking(facts):
    lc = _derive(AgentResult(conclusion="x", issues=[
        ResultIssue(title="Where is the remaining contract quantity stored in JD Edwards?", owner="business", blocks=True)]))
    tech = next(i for i in lc.issues if i.source == "architect")
    assert tech.owner == "jade" and tech.blocks                                  # JADE's work, still blocking
    assert lc.next_action.owner == "product_manager" and lc.next_action.action == "rerun_solutioning"
    assert lc.health == "blocked"
    assert "approve_exact_change" in lc.action_blockers                          # no approval button offered
    assert not any(i.owner == "domain_owner" and i.resolution is None for i in lc.issues if i.source == "architect")


def test_a_ready_design_offers_approval_on_every_surface(facts):
    lc = _derive(AgentResult(conclusion="x"))
    assert "approve_exact_change" in lc.allowed_actions and "approve_exact_change" not in lc.action_blockers


def test_readiness_refusal_names_the_blocker_and_its_owner(facts):
    class C:
        lifecycle = _derive(AgentResult(conclusion="x", issues=[
            ResultIssue(title="Which GL account should freight revenue post to?", owner="business", who="Finance",
                        blocks=True, options=["4100", "4150"])]))
    reason = readiness.refusal(C)
    assert reason and "Which GL account" in reason and "Finance" in reason


def test_endpoints_refuse_an_approval_the_screen_does_not_offer(client, monkeypatch):
    """The same readiness rule on the endpoint: a design with an open technical investigation cannot be
    approved through the API either; once nothing blocks, the same call succeeds."""
    from . import test_architect_discovery as tad
    from ._discovery import ready_company
    from .conftest import headers
    from .test_stage1_execution_safeguards import _approved_story, _full_scope, _propose, _save_scope

    base = tad._summary

    def with_issue(evidence):
        s = base(evidence)
        s["result"] = {"conclusion": "Set the default order type.", "issues": [{
            "title": "Which version of P4210 is used for order entry today?", "owner": "decision", "blocks": True,
            "consequence": "The change targets the wrong version otherwise.", "action": "Read it in DEV."}]}
        return s

    ready_company(client, "vdb")
    _save_scope(client, "vdb", _full_scope())
    _approved_story("S-READY-1")
    monkeypatch.setattr(tad, "_summary", with_issue)
    tad._run(monkeypatch, lambda tools: (_propose("S-READY-1"), {})[1], story="S-READY-1")
    change = client.get("/changes/S-READY-1", headers=headers("vdb")).json()
    issue = next(i for i in change["lifecycle"]["issues"] if i["source"] == "architect")
    assert issue["owner"] == "jade" and issue["blocks"]
    assert "approve_exact_change" in change["lifecycle"]["actionBlockers"]
    r = client.post("/changes/S-READY-1/approve-change", headers=headers("vdb"), json={"note": "ok"})
    assert r.status_code == 409 and "Not ready to authorise" in r.json()["detail"], r.text
    # A person cannot tick off JADE's own investigation; the Architect's next run settles it.
    r = client.post(f"/changes/S-READY-1/issues/{issue['id']}/resolution", headers=headers("vdb"),
                    json={"status": "resolved", "answer": ""})
    assert r.status_code == 409
    monkeypatch.setattr(tad, "_summary", base)
    tad._run(monkeypatch, lambda tools: (_propose("S-READY-1"), {})[1], story="S-READY-1")
    r = client.post("/changes/S-READY-1/approve-change", headers=headers("vdb"), json={"note": "ok"})
    assert r.status_code == 200, r.text
