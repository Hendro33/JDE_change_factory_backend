"""
Tests for POST /changes/{id}/enhance and orchestration_driver.py.

The Claude Agent SDK itself is mocked (via monkeypatching
claude_agent_sdk.query) so these tests are deterministic and don't
spend real API calls -- but everything downstream of that one function
is exercised for real: the actual backlog.py/evidence.py file stores,
the actual change_service assembler, the actual customer-link sidecar.
The fake query() below simulates exactly what a real Check Agent
success does: it calls the real propose_to_backlog function itself,
the same MCP tool the real subagent would call.

Starlette's TestClient runs FastAPI BackgroundTasks to completion
before client.post(...) returns (it drives the whole ASGI response
cycle synchronously), so these tests can call the endpoint and assert
the final state immediately -- no polling needed here, unlike a real
browser client against a real server.
"""

from __future__ import annotations

import json
import re

import claude_agent_sdk as sdk
import pytest
from jde_mcp_server import backlog

from .conftest import headers


def _sys(subagent_type: str) -> sdk.SystemMessage:
    return sdk.SystemMessage(subtype="task_started", data={"subagent_type": subagent_type})


def _result(text: str, is_error: bool = False) -> sdk.ResultMessage:
    return sdk.ResultMessage(
        subtype="success" if not is_error else "error",
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=3,
        session_id="test-session",
        result=text,
    )


def _story_id_from_prompt(prompt: str) -> str:
    # Mirrors _build_prompt's exact wording -- extracts whatever real
    # id orchestration_driver.py actually asked the pipeline to use,
    # so these fakes stay correct regardless of how that id was
    # generated (random for real intake, stable for pilot seed data).
    m = re.search(r"story_id to use throughout, in every tool call: (\S+)", prompt)
    assert m, "fake query() could not find story_id in the prompt -- did _build_prompt's wording change?"
    return m.group(1)


def _summary_for(story_id: str) -> dict:
    return {
        "story_id": story_id,
        "user_story": {
            "statement": "As a sales team member, I want the requested delivery date to default to 7 working days from the order date, so that I stop manually correcting it.",
            "business_context": "Sales order entry currently defaults delivery date to today, which is wrong for bicycles not immediately available.",
            "acceptance_criteria": [{"id": "AC1", "text": "New sales order defaults delivery date to order date + 7 working days.", "verified_by": "T1"}],
            "test_script": [{"id": "T1", "action": "Create a new sales order", "expected": "Delivery date defaults to order date + 7 working days"}],
            "open_questions": ["Should public holidays be excluded from the working-day count?"],
            "quality_status": "passed",
            "revision_count": 0,
        },
        "business_impact": {
            "financial_impact": "", "operational_reach": "Sales team, every order for unavailable stock",
            "risk_compliance": "", "strategic_alignment": "", "urgency": "",
        },
        "rough_complexity_signal": "Low",
        "check_outcome": "proposed_to_backlog",
        "failed_criteria": [],
    }


def _revision_summary_for(story_id: str) -> dict:
    summary = _summary_for(story_id)
    summary["user_story"] = {**summary["user_story"], "quality_status": "needs_revision", "revision_count": 1}
    summary["check_outcome"] = "needs_revision"
    summary["failed_criteria"] = ["Criterion 3: acceptance criteria not fully testable yet."]
    return summary


async def _fake_query_success(*, prompt, options):
    story_id = _story_id_from_prompt(prompt)
    summary = _summary_for(story_id)
    yield _sys("receive-agent")
    yield _sys("improve-agent")
    yield _sys("check-agent")
    # Simulates the real Check Agent's own tool call -- the actual MCP
    # tool, not a re-implementation of it.
    backlog.propose_to_backlog(
        story_id,
        summary["user_story"]["statement"],
        summary["business_impact"],
        summary["rough_complexity_signal"],
        source="Support / Topdesk, Topdesk T001",
    )
    yield _result(f"```json\n{json.dumps(summary)}\n```")


async def _fake_query_needs_revision(*, prompt, options):
    story_id = _story_id_from_prompt(prompt)
    yield _sys("receive-agent")
    yield _sys("improve-agent")
    yield _sys("check-agent")
    yield _sys("improve-agent")
    yield _sys("check-agent")
    yield _result(f"```json\n{json.dumps(_revision_summary_for(story_id))}\n```")


async def _fake_query_sdk_error(*, prompt, options):
    yield _sys("receive-agent")
    yield _result("something went wrong", is_error=True)


def _seed_t001(client) -> str:
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
    assert r.status_code == 201
    return r.json()["id"]


def test_successful_enhancement_promotes_to_a_real_backlog_story(client, monkeypatch):
    monkeypatch.setattr(sdk, "query", _fake_query_success)
    request_id = _seed_t001(client)

    r = client.post(f"/changes/{request_id}/enhance", headers=headers(customer="bwm"))
    assert r.status_code == 202

    r = client.get(f"/changes/{request_id}", headers=headers(customer="bwm"))
    change = r.json()
    assert change["state"] == "BACKLOG_READY"
    assert change["processingStage"] == "done"
    assert change["userStory"]["qualityStatus"] == "passed"
    assert len(change["userStory"]["acceptanceCriteria"]) == 1
    assert change["userStory"]["openQuestions"] == [
        "Should public holidays be excluded from the working-day count?"
    ]
    assert change["businessImpact"]["operationalReach"] == "Sales team, every order for unavailable stock"

    # It's now a real backlog.py record -- appears in /backlog too.
    r = client.get("/backlog", headers=headers(customer="bwm"))
    assert any(c["id"] == request_id for c in r.json())


def test_enhancement_preserves_original_ticket_text_unchanged(client, monkeypatch):
    monkeypatch.setattr(sdk, "query", _fake_query_success)
    request_id = _seed_t001(client)
    client.post(f"/changes/{request_id}/enhance", headers=headers(customer="bwm"))

    r = client.get(f"/changes/{request_id}", headers=headers(customer="bwm"))
    # originalRequest still carries the raw customer text, distinct
    # from the AI-generated userStory -- the two are never merged.
    assert r.json()["originalRequest"] == "When our sales team enters a new sales order..."
    assert r.json()["userStory"]["statement"] != r.json()["originalRequest"]


def test_needs_revision_outcome_is_shown_honestly_not_forced_to_pass(client, monkeypatch):
    monkeypatch.setattr(sdk, "query", _fake_query_needs_revision)
    request_id = _seed_t001(client)

    r = client.post(f"/changes/{request_id}/enhance", headers=headers(customer="bwm"))
    assert r.status_code == 202

    r = client.get(f"/changes/{request_id}", headers=headers(customer="bwm"))
    change = r.json()
    # Not promoted to backlog -- Check Agent did not pass it.
    assert change["state"] == "REFINING"
    assert change["userStory"]["qualityStatus"] == "needs_revision"
    assert change["userStory"]["revisionCount"] == 1

    r = client.get("/backlog", headers=headers(customer="bwm"))
    assert not any(c["id"] == request_id for c in r.json())


def test_orchestration_failure_is_recorded_not_silently_dropped(client, monkeypatch):
    monkeypatch.setattr(sdk, "query", _fake_query_sdk_error)
    request_id = _seed_t001(client)

    client.post(f"/changes/{request_id}/enhance", headers=headers(customer="bwm"))

    r = client.get(f"/changes/{request_id}", headers=headers(customer="bwm"))
    change = r.json()
    assert change["state"] == "RECEIVED"  # unchanged -- nothing was actually refined
    assert change["processingStage"] == "failed"
    assert change["processingError"] is not None


def test_cannot_start_a_second_enhancement_while_one_is_running(client, monkeypatch):
    started = {"count": 0}

    async def _hanging_query(*, prompt, options):
        started["count"] += 1
        yield _sys("receive-agent")
        # Never yields a ResultMessage -- simulates "still running".
        import asyncio

        await asyncio.Event().wait()

    monkeypatch.setattr(sdk, "query", _hanging_query)
    request_id = _seed_t001(client)

    # Directly exercise the run-in-progress guard rather than the full
    # endpoint (which would hang this test): start a run record by hand.
    from jde_api_service.services.registry import get_enhancement_run_service

    get_enhancement_run_service().start(request_id)

    r = client.post(f"/changes/{request_id}/enhance", headers=headers(customer="bwm"))
    assert r.status_code == 409


def test_enhance_requires_the_change_request_to_belong_to_the_caller(client, monkeypatch):
    monkeypatch.setattr(sdk, "query", _fake_query_success)
    request_id = _seed_t001(client)

    r = client.post(f"/changes/{request_id}/enhance", headers=headers(user="u-hendro", customer="vdb"))
    assert r.status_code == 404


def test_enhance_unknown_change_id_is_404(client):
    r = client.post("/changes/CR-does-not-exist/enhance", headers=headers(customer="bwm"))
    assert r.status_code == 404
