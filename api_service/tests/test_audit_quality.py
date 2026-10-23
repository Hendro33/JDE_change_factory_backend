"""Corrections from the 23 Oct audit, requirement and design quality
(F03, F04, F05, F06). The agents are scripted at the model boundary; the
deterministic checks run for real."""

from __future__ import annotations

import asyncio
import json

import claude_agent_sdk as sdk
import pytest

from .conftest import headers

GUIDE_SOURCE = ("The order desk keeps phoning purchasing to ask whether an item is available at another branch. "
                "Write a short guide for the order desk. Check if the standard inquiry supports availability per "
                "branch, and describe the steps in it.")


def _story(criterion: str, kept_as: str | None = None, trace: list | None = None) -> dict:
    return {
        "statement": "As an order desk clerk I want a guide to availability per branch so that I stop phoning purchasing.",
        "acceptance_criteria": [criterion],
        "open_questions": [], "assumptions": [], "business_rules": [],
        "source_conditions": [{"source": "if the standard inquiry supports availability per branch",
                               "kept_as": kept_as or criterion}],
        "requirement_trace": trace if trace is not None else [
            {"criterion": criterion, "source_quote": "Check if the standard inquiry supports availability per branch"}],
    }


# ---------------------------------------------------------------------
# F04 -- source fidelity
# ---------------------------------------------------------------------
def test_f04_a_condition_turned_into_a_conclusion_is_caught():
    from jde_api_service.services import fidelity

    # The audited defect (CR-011be0ca): "if the standard inquiry supports this" became a required conclusion.
    bad = _story("The guide concludes that standard JD Edwards already meets the need for availability per branch.")
    failures = fidelity.check(GUIDE_SOURCE, bad)
    assert any("turns it into a settled statement" in f for f in failures), failures
    good = _story("The guide states whether the standard inquiry supports availability per branch, with the steps.")
    assert fidelity.check(GUIDE_SOURCE, good) == []


def test_f04_unaccounted_conditions_untraced_criteria_and_invented_scope_are_caught():
    from jde_api_service.services import fidelity

    s = _story("The guide states whether the standard inquiry supports availability per branch.")
    s["source_conditions"] = []
    assert any("does not account for" in f for f in fidelity.check(GUIDE_SOURCE, s))
    s = _story("The guide states whether the standard inquiry supports availability per branch.", trace=[])
    assert any("without a source trace" in f for f in fidelity.check(GUIDE_SOURCE, s))
    s = _story("The guide states whether the standard inquiry supports availability per branch.", trace=[
        {"criterion": "The guide states whether the standard inquiry supports availability per branch.",
         "source_quote": "The guide must also cover credit limits for every customer"}])
    assert any("Invented scope" in f for f in fidelity.check(GUIDE_SOURCE, s))
    # A criterion derived for testability is fine when it says why.
    s = _story("The guide states whether the standard inquiry supports availability per branch.", trace=[
        {"criterion": "The guide states whether the standard inquiry supports availability per branch.",
         "derived_because": "needed to test that the guide answers the order desk's question"}])
    assert fidelity.check(GUIDE_SOURCE, s) == []


def test_f04_the_story_form_is_not_mistaken_for_a_condition():
    from jde_api_service.services import fidelity

    assert fidelity.conditional_clauses("Als orderverwerker wil ik de beschikbaarheid per vestiging zien.") == []
    assert fidelity.conditional_clauses("Toon de voorraad, tenzij het artikel geblokkeerd is.") == [
        "tenzij het artikel geblokkeerd is"]


def test_f04_propose_to_backlog_refuses_a_story_that_changed_the_meaning(client):
    from jde_mcp_server import backlog

    from jde_api_service.ai.project_tools import FidelityRefused, ProjectTools
    from jde_api_service.models.change_request import ChangeRequestCreate
    from jde_api_service.services.registry import get_change_request_service

    cr = get_change_request_service().create_direct(
        ChangeRequestCreate(title="Availability guide", business_source="Business", raw_content=GUIDE_SOURCE),
        customer_id="vdb", requester="Hendro")
    tools = ProjectTools(company_id="vdb", story_id=cr.id)
    bad = _story("The guide concludes that standard JD Edwards already meets the need for availability per branch.")
    with pytest.raises(FidelityRefused) as exc:
        tools.propose_to_backlog({"story_id": cr.id, "user_story": bad["statement"], "business_impact": {},
                                  "rough_complexity_signal": "Low", "source": "a retelling", "story": bad})
    assert "settled statement" in str(exc.value)
    assert backlog._load(cr.id) is None  # noqa: SLF001 -- nothing was proposed
    good = _story("The guide states whether the standard inquiry supports availability per branch, with the steps.")
    tools.propose_to_backlog({"story_id": cr.id, "user_story": good["statement"], "business_impact": {},
                              "rough_complexity_signal": "Low", "source": GUIDE_SOURCE, "story": good})
    assert backlog._load(cr.id)["status"] == "backlog"  # noqa: SLF001


# ---------------------------------------------------------------------
# F03 / F05 / F06 -- the design is executable before it can be authorised
# ---------------------------------------------------------------------
def _design(client, monkeypatch, story: str, *, route: str, found: str = "", sequence: list[str],
            human: list[str] | None = None, changes: list[dict] | None = None, answered: list[dict] | None = None,
            customer: str = "vdb") -> dict:
    from jde_api_service.config import settings
    from jde_api_service.services import architecture_driver
    from jde_api_service.services.registry import get_architecture_review_service

    summary = {
        "architect_decision": {"recommended_route": route, "confidence": 0.7, "existing_functionality_found": found,
                               "alternatives_considered": [], "objects_affected": [], "dependencies_and_conflicts": [],
                               "rollback_strategy": "remove the new version"},
        "implementation_spec": {"sequence": sequence, "required_mcp_operations": [],
                                "human_actions_required": human or [], "validation_approach": "run it in DEV"},
        "evidence": {},
        "result": {"conclusion": "JADE recommends this route.", "changes": changes or [], "issues": [],
                   "uncertainty": [], "answered_questions": answered or []},
    }

    async def fake_query(prompt, options):
        yield sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=2,
                                session_id="t", result="```json\n" + json.dumps(summary) + "\n```")

    monkeypatch.setattr(sdk, "query", fake_query)
    service = get_architecture_review_service()
    asyncio.run(architecture_driver.run_architecture_review(story_id=story, repo_root=settings.repo_root,
                                                            run_service=service, customer_id=customer,
                                                            initiated_by="u-hendro"))
    assert service.get(story).stage == "done", service.get(story).error
    return client.get(f"/changes/{story}", headers=headers(customer)).json()


def _blocking(lc: dict) -> list[dict]:
    return [i for i in lc["issues"] if i["blocks"] and not i.get("resolution") and i["source"] == "design_quality"]


def test_f03_custom_development_is_not_recommended_before_standard_is_established(client, monkeypatch):
    from .test_stage1_execution_safeguards import _approved_story

    _approved_story("S-Q-STD")
    c = _design(client, monkeypatch, "S-Q-STD", route="Technical Agent",
                found="Standard hold enforcement at pick and ship has not been confirmed in DEV; build the hooks "
                      "regardless.",
                sequence=["Technical Agent changes R5542565 to check the lot hold before printing"])
    lc = c["lifecycle"]
    issues = _blocking(lc)
    assert issues and "Standard behaviour is not established" in issues[0]["title"]
    assert lc["health"] == "blocked" and lc["nextAction"]["action"] == "rerun_solutioning"


def test_f03_an_unknown_target_is_marked_and_blocks_authorisation(client, monkeypatch):
    from .test_stage1_execution_safeguards import _approved_story

    _approved_story("S-Q-TGT")
    c = _design(client, monkeypatch, "S-Q-TGT", route="Functional Agent", sequence=["Functional Agent sets the code"],
                changes=[{"what": "Hold code UDC 41/L", "current": "H1", "proposed": "unknown", "status": "verified"}])
    row = c["architectDecision"]["result"]["changes"][0]
    assert row["status"] == "verified" and row["targetKnown"] is False   # current verified, target not known
    assert any("target value" in i["title"] for i in c["lifecycle"]["issues"] if i["source"] == "design_quality")


def test_f06_two_executors_for_one_build_and_new_objects_for_the_technical_agent(client, monkeypatch):
    from .test_stage1_execution_safeguards import _approved_story

    _approved_story("S-Q-EXE")
    c = _design(client, monkeypatch, "S-Q-EXE", route="Mixed",
                sequence=["Set processing option 1 of P41202 version ZJDE0001",
                          "Technical Agent designs and builds a new UBE report R5541999 for the CSV export"],
                human=["A developer builds the new report R5541999 in OMW"])
    titles = [i["title"] for i in _blocking(c["lifecycle"])]
    assert any("two different executors" in t for t in titles), titles
    assert any("create a new object" in t for t in titles), titles
    assert c["lifecycle"]["health"] == "blocked"


def test_f06_scope_decisions_come_before_authorisation_not_in_the_handoff(client, monkeypatch):
    from jde_api_service.delivery import handoff
    from jde_api_service.services.registry import get_change_service

    from .test_stage1_execution_safeguards import _approved_story

    _approved_story("S-Q-DEC")
    c = _design(client, monkeypatch, "S-Q-DEC", route="Human Implementation",
                sequence=["Approve the combined design", "Determine the rate per branch with Finance",
                          "Configure the branch mapping table in DEV as specified"])
    titles = [i["title"] for i in _blocking(c["lifecycle"])]
    assert any("Determine the rate per branch" in t for t in titles), titles
    assert not any("Approve the combined design" in t for t in titles)
    change = get_change_service().get_for_customer("S-Q-DEC", "vdb")
    assert [i["text"] for i in handoff.items_for(change)] == ["Configure the branch mapping table in DEV as specified"]


def test_f05_technical_questions_go_to_jade_and_discovery_retires_them(client, monkeypatch):
    from jde_mcp_server import backlog

    from .test_stage1_execution_safeguards import _approved_story

    _approved_story("S-Q-QST")
    rec = backlog._load("S-Q-QST")  # noqa: SLF001
    detail = {"statement": "As a buyer I want a CSV of stock per branch", "acceptance_criteria": [{"id": "AC1", "text": "The CSV lists stock"}]}
    detail["open_questions"] = ["Which stocking type value marks a non-stock item?",
                                "Should the export include items with zero stock?"]
    rec["story_detail"] = detail
    backlog._save("S-Q-QST", rec)  # noqa: SLF001
    c = _design(client, monkeypatch, "S-Q-QST", route="Functional Agent", sequence=["Functional Agent sets the option"],
                answered=[{"question": "Which stocking type value marks a non-stock item?",
                           "answer": "Stocking type N (UDC 41/I) marks non-stock items.", "evidence": "OBS-12"}])
    story_issues = {i["title"]: i for i in c["lifecycle"]["issues"] if i["source"] == "story"}
    tech = story_issues["Which stocking type value marks a non-stock item?"]
    assert tech["owner"] == "jade" and tech["resolution"]["by"] == "JADE (discovery)"
    assert "Stocking type N" in tech["resolution"]["answer"]
    biz = story_issues["Should the export include items with zero stock?"]
    # No domain yet, so the Application Manager has it first (register C3).
    assert biz["owner"] == "product_manager" and "no domain yet" in biz["ownerLabel"] and biz["resolution"] is None


