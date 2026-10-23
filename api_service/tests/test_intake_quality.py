"""Finding 3 (post-step-3 review): the requirements-generation pipeline.

Traced causes, each pinned here so it cannot silently come back:
  * the reviewing (Check) agent was never given the source request, so it
    could not check traceability or questions the source already answers;
  * benefit and impact ratings were never asked of the drafting (Improve)
    agent -- only of the end-of-run summary, with "use null";
  * the story Jade showed was the orchestrator's end-of-run retelling,
    not the story the Check Agent actually passed;
  * nothing recorded the hand-offs, so none of this could be traced.
The SDK is scripted at its boundary (as in test_orchestration.py); the
backlog, change assembly and run records are real. Scenarios use three
different tickets, not only the regression example CON-35.
"""

from __future__ import annotations

import json
import os
import re

import claude_agent_sdk as sdk
import pytest
from jde_mcp_server import backlog

from .conftest import headers

SOURCES = {
    "CON-35": "QA puts lots on status T (Hold for Testing) or Q (Under Quarantine). Last month a lot on hold was still "
              "picked for a customer order. We want JDE to make it impossible to sell or ship stock from a lot that is "
              "on hold.\n\nIs this something JDE can already do, or does it need a change?\n\nRequested by: Quality manager",
    "CON-37": "Every Monday the production planner needs to know how many Cro-Moly frames (2001, 2002, 2003, 2004) we have "
              "on hand in each warehouse (branch plants 10, 20 and 30), to plan the touring bike assembly (220, 221, 222).\n\n"
              "Today someone checks every item in every warehouse by hand. We would like this as one list, preferably "
              "emailed or available every Monday morning.\n\nRequested by: Production planner",
    "CON-38": "Branches order office and workshop supplies from suppliers such as Supplies Unlimited (3090), Custer Office "
              "Supplies (3107) and Waco Paper Supplies (3105) without any approval. Finance wants every purchase order above "
              "2,500 USD approved by the branch manager before it is sent to the supplier; orders below that can go straight "
              "out.\n\nRequested by: Financial controller, company 00001",
}

CHECKED = {
    "CON-35": {"statement": "As a quality manager I want stock in lots on status T or Q to be impossible to sell or ship, "
                            "so that no held lot reaches a customer.",
               "business_context": "Last month a lot on hold was picked for a customer order.",
               "business_impact_rating": "High", "business_benefit_rating": "Medium",
               "rating_basis": "A held lot reached a customer order (quality risk across sales and shipping); the benefit "
                               "is preventing a repeat.",
               "acceptance_criteria": [{"id": "AC1", "text": "A lot on status T or Q cannot be committed to a sales order.",
                                        "verified_by": "T1"}],
               "test_script": [{"id": "T1", "action": "Enter an order for an item whose only lot is on status Q",
                                "expected": "The lot is not committed"}],
               "open_questions": [], "assumptions": [], "business_rules": ["Hold statuses: T and Q."]},
    "CON-37": {"statement": "As a production planner I want a Monday list of frames 2001-2004 on hand per branch plant "
                            "10, 20 and 30, so that I can plan assembly of 220, 221 and 222 without manual checks.",
               "business_impact_rating": "Low", "business_benefit_rating": "Medium",
               "rating_basis": "One planner, weekly manual check of 4 items in 3 warehouses.",
               "acceptance_criteria": [{"id": "AC1", "text": "The list shows on-hand per item and branch.", "verified_by": "T1"}],
               "test_script": [{"id": "T1", "action": "Run the list", "expected": "12 rows, one per item and branch"}],
               "open_questions": ["Emailed, or available to open? The source allows either."]},
    "CON-38": {"statement": "As a financial controller I want purchase orders above 2,500 USD approved by the branch "
                            "manager before they are sent, so that spending is controlled.",
               "business_impact_rating": "Medium", "business_benefit_rating": "Medium",
               "rating_basis": "Every branch's supply purchasing; finance control over spend above 2,500 USD.",
               "acceptance_criteria": [{"id": "AC1", "text": "A PO above 2,500 USD waits for branch manager approval.",
                                        "verified_by": "T1"}],
               "test_script": [{"id": "T1", "action": "Enter a PO of 3,000 USD", "expected": "Status awaiting approval"}],
               "open_questions": []},
}


def _story_id(prompt: str) -> str:
    return re.search(r"story_id to use throughout, in every tool call: (\S+)", prompt).group(1)


def _source(prompt: str) -> str:
    return re.search(r'Raw draft text .*?\n"""(.*?)"""', prompt, re.DOTALL).group(1)


def _task(n: int, agent: str, prompt: str) -> sdk.AssistantMessage:
    return sdk.AssistantMessage(content=[sdk.ToolUseBlock(id=f"t{n}", name="Task",
                                                          input={"subagent_type": agent, "prompt": prompt})], model="m")


def _done(n: int, text: str) -> sdk.UserMessage:
    return sdk.UserMessage(content=[sdk.ToolResultBlock(tool_use_id=f"t{n}", content=text)])


def _result(text: str) -> sdk.ResultMessage:
    return sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=5,
                             session_id="s", result=text)


def _pipeline(ticket: str, *, revision: bool = False, drop_source_for: str = ""):
    """A scripted run that behaves as the corrected instructions require: every
    hand-off carries the verbatim source; the Check Agent passes the story it
    checked; the end summary is a lossy retelling (ratings dropped, a question
    added) -- which Jade must not show."""
    async def query(*, prompt, options):
        sid, src = _story_id(prompt), _source(prompt)
        story = CHECKED[ticket]
        order = ["receive-agent", "improve-agent", "check-agent"] + (["improve-agent", "check-agent"] if revision else [])
        for n, agent in enumerate(order):
            given = f"Source request (verbatim)\n{src}\n\nStory:\n{json.dumps(story)}" if agent != drop_source_for \
                else f"Story:\n{json.dumps(story)}"
            yield _task(n, agent, given)
            yield _done(n, "criterion 10 failed: question answered in the source" if revision and n == 2
                        else json.dumps(story))
        backlog.propose_to_backlog(sid, story["statement"], {"operational_reach": "stated"}, "Low", "Jira", story)
        retold = {**story, "business_impact_rating": None, "business_benefit_rating": None, "rating_basis": "",
                  "open_questions": story.get("open_questions", []) + ["Which lot statuses count as on hold?"]}
        summary = {"story_id": sid, "user_story": {**retold, "quality_status": "passed", "revision_count": int(revision)},
                   "business_impact": {}, "rough_complexity_signal": "Low", "check_outcome": "proposed_to_backlog",
                   "failed_criteria": []}
        yield _result(f"```json\n{json.dumps(summary)}\n```")
    return query


def _intake(client, monkeypatch, ticket: str, **kw) -> tuple[str, dict]:
    monkeypatch.setattr(sdk, "query", _pipeline(ticket, **kw))
    r = client.post("/change-requests", headers=headers(customer="bwm"),
                    json={"title": ticket, "businessSource": "Jira", "sourceReference": ticket, "rawContent": SOURCES[ticket]})
    rid = r.json()["id"]
    assert client.post(f"/changes/{rid}/enhance", headers=headers(customer="bwm")).status_code == 202
    return rid, client.get(f"/changes/{rid}", headers=headers(customer="bwm")).json()


def _trace(story_id: str) -> list[dict]:
    from jde_api_service.ai import runtime

    run = next(r for r in runtime.list_runs("bwm", 200) if r["story_id"] == story_id and r["driver"] == "orchestration_driver")
    return runtime.run_output("bwm", run["run_id"])["trace"]


@pytest.mark.parametrize("ticket", ["CON-35", "CON-37", "CON-38"])
def test_the_story_shown_is_the_one_the_check_agent_passed(client, monkeypatch, ticket):
    rid, change = _intake(client, monkeypatch, ticket)
    us = change["userStory"]
    want = CHECKED[ticket]
    assert us["statement"] == want["statement"]
    # Ratings and their basis survive; the end-of-run retelling's extra question does not appear.
    assert us["businessImpactRating"] == want["business_impact_rating"]
    assert us["businessBenefitRating"] == want["business_benefit_rating"]
    assert us["ratingBasis"] == want["rating_basis"]
    assert "Which lot statuses count as on hold?" not in us["openQuestions"]
    assert change["ratings"]["businessBenefit"]["status"] == "proposed"


def test_every_hand_off_is_traced_with_the_complete_source(client, monkeypatch):
    from jde_api_service.services.orchestration_driver import trace_findings

    rid, _ = _intake(client, monkeypatch, "CON-35", revision=True)
    trace = _trace(rid)
    f = trace_findings(trace)
    assert f["agents"] == ["receive-agent", "improve-agent", "check-agent", "improve-agent", "check-agent"]
    assert f["every_agent_had_source"] and f["check_rounds"] == 2 and f["revised_between_checks"] is True
    assert all(t["result"] for t in trace)   # each agent's answer is kept in full


def test_a_hand_off_without_the_source_is_visible_in_the_trace(client, monkeypatch):
    from jde_api_service.services.orchestration_driver import trace_findings

    rid, _ = _intake(client, monkeypatch, "CON-37", drop_source_for="check-agent")
    f = trace_findings(_trace(rid))
    assert not f["every_agent_had_source"] and f["missing_source"] == ["check-agent"]


def test_the_instructions_require_source_ratings_and_answered_questions():
    from jde_api_service.services.orchestration_driver import _build_prompt

    prompt = _build_prompt("S1", "Jira", "the text")
    assert "Source request (verbatim)" in prompt and "Never summarise" in prompt
    assert "Use null when evidence is insufficient" not in prompt
    root = os.path.join(os.path.dirname(__file__), "..", "..", ".claude", "agents")
    improve = open(os.path.join(root, "improve-agent.md")).read()
    check = open(os.path.join(root, "check-agent.md")).read()
    assert "business_benefit_rating" in improve and "rating_basis" in improve
    assert "search the source for the answer" in improve
    assert "No question the source already answers" in check and "Ratings are proposed" in check
    assert "Source request" in check and "story: the complete structured story" in check
