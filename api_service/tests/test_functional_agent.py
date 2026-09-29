"""
The Functional Agent: the JD Edwards configuration specialist, active in the
solutioning run after the Architect. Through the real architecture driver
and runtime (the model replaced by a script that calls the same in-process
tools the agents get):

- the run defines both subagents from the customer's Start-up Packs, and only
  the Functional Agent may propose a change;
- the Functional Agent reads the customer's approved configuration with
  get_engagement_scope and proposes a multi-item configuration change set,
  which the design's hand-off names;
- the tools are bound to the run's story, and a refused set is reported to
  the agent, never raised;
- without a Functional Agent pack the run is blocked with a clear reason.
"""

from __future__ import annotations

import json

import pytest

from .conftest import headers
from .test_architect_discovery import STORY, _run
from .test_configuration_change_sets import NEW_ORDER_TYPE, READS, SCOPE
from .test_stage1_execution_safeguards import _approved_story, _save_scope
from ._discovery import ready_company


@pytest.fixture()
def ready(client):
    ready_company(client, "vdb", approvedReads=READS)
    _save_scope(client, "vdb", SCOPE)
    _approved_story(STORY)
    return client


def _capture_project_tools(monkeypatch) -> dict:
    from jde_api_service.ai import project_tools

    seen: dict = {}
    real = project_tools.ProjectTools.sdk_server

    def spy(self):
        seen["tools"] = self
        return real(self)

    monkeypatch.setattr(project_tools.ProjectTools, "sdk_server", spy)
    return seen


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def test_the_functional_agent_proposes_a_configuration_change_set_in_the_solutioning_run(ready, monkeypatch):
    from jde_mcp_server.design_baseline import get_design_baseline

    seen = _capture_project_tools(monkeypatch)
    scope_seen = {}

    def script(discovery):
        pt = seen["tools"]
        # The Functional Agent confirms the target in DEV, checks what the customer allows ...
        discovery.read("udc_values", "00/DT")
        scope_seen.update(_payload(pt.call("get_engagement_scope", pt.get_engagement_scope, {"story_id": STORY})))
        # ... and proposes the whole set once.
        r = pt.call("propose_change", pt.propose_change, {"story_id": STORY, "operation": NEW_ORDER_TYPE,
                                                          "capability_id": "configuration_change_set"})
        assert not r.get("is_error"), r
        return {}

    tools, run = _run(monkeypatch, script)
    # What the Functional Agent saw: the customer's approved configuration, read-only.
    assert [e["target"] for e in scope_seen["approved_configuration"]] == ["00/DT", "F40039:DCTO=SW|SX", "R42565|CIQ0001"]
    assert scope_seen["approved_tests"][0]["orchestration"] == "ORCH_SO"
    # The design's hand-off names the change set it proposed.
    handoff = get_design_baseline(STORY)
    assert handoff["change"]["status"] == "pending"
    assert [i["capability_id"] for i in handoff["change"]["operation"]["items"]] == [
        "udc_value_maintenance", "document_type_definition", "processing_option_update", "batch_version_data_selection"]
    change = ready.get(f"/changes/{STORY}", headers=headers("vdb")).json()
    assert [i["id"] for i in change["exactChange"]["items"]] == ["I1", "I2", "I3", "I4"]


def test_the_run_defines_both_agents_from_their_packs_and_only_the_functional_agent_proposes(ready, monkeypatch):
    import claude_agent_sdk as sdk

    from jde_api_service.services import architecture_driver

    captured = {}

    async def fake_query(prompt, options):
        captured["options"], captured["prompt"] = options, prompt
        from .test_architect_discovery import _result, _summary

        yield _result(_summary({}))

    monkeypatch.setattr(sdk, "query", fake_query)
    from jde_api_service.config import settings
    from jde_api_service.services.registry import get_architecture_review_service
    import asyncio

    asyncio.run(architecture_driver.run_architecture_review(
        story_id=STORY, repo_root=settings.repo_root, run_service=get_architecture_review_service(), customer_id="vdb",
        initiated_by="u-hendro"))
    agents = captured["options"].agents
    assert set(agents) == {"architect", "functional-agent"}
    fa, arch = agents["functional-agent"], agents["architect"]
    assert "mcp__jde-change-factory__propose_change" in fa.tools
    assert "mcp__jde-change-factory__get_engagement_scope" in fa.tools
    assert "mcp__jde-change-factory__propose_change" not in arch.tools
    assert "mcp__jde-change-factory__resolve_without_change" in arch.tools
    # Its instructions are the configuration specialist's, not a single processing option.
    assert "configuration specialist" in fa.prompt and "F40039" in fa.prompt
    assert "## Expected outputs" in fa.prompt or "Report back" in fa.prompt
    # Neither can reach anything that changes JD Edwards.
    for a in (fa, arch):
        assert not any(t.startswith("mcp__jde-mcp") or "apply" in t for t in a.tools)
    assert "functional-agent" in captured["prompt"] and "configuration_change_set" in captured["prompt"]


def test_the_tools_are_bound_to_the_story_and_a_refusal_is_reported_to_the_agent(ready, monkeypatch):
    seen = _capture_project_tools(monkeypatch)
    results = {}

    def script(discovery):
        pt = seen["tools"]
        results["other"] = pt.call("get_engagement_scope", pt.get_engagement_scope, {"story_id": "S-SOMEONE-ELSE"})
        bad = {"tool": "configuration_change_set", "items": [
            {"capability_id": "document_type_definition", "table": "F40039", "key": {"DCTO": "SZ"}, "action": "add",
             "values": {"DCT4": "SO"}}]}
        results["refused"] = pt.call("propose_change", pt.propose_change,
                                     {"story_id": STORY, "operation": bad, "capability_id": "configuration_change_set"})
        return {}

    _run(monkeypatch, script)
    assert results["other"]["is_error"] and "only" in _payload(results["other"])["error"]
    assert results["refused"]["is_error"] and _payload(results["refused"])["refused"]
    assert "not in company" in _payload(results["refused"])["error"]
    from jde_mcp_server.design_baseline import get_design_baseline

    assert get_design_baseline(STORY)["change"]["status"] == "none"  # nothing was proposed


def test_without_a_functional_agent_pack_the_solutioning_run_is_blocked_with_the_reason(ready, monkeypatch):
    import asyncio

    import claude_agent_sdk as sdk

    from jde_api_service.ai import packs
    from jde_api_service.config import settings
    from jde_api_service.services import architecture_driver
    from jde_api_service.services.registry import get_architecture_review_service

    from . import _ai

    _ai.configure("vdb")
    packs.unassign("vdb", "functional-agent", actor="test")

    async def never(prompt, options):  # pragma: no cover -- must not be reached
        raise AssertionError("the model must not be called")
        yield

    monkeypatch.setattr(sdk, "query", never)
    from jde_api_service.ai import runtime

    # The real resolution, without the test helper that assigns missing packs.
    monkeypatch.setattr(runtime, "prepare", _prepare)
    service = get_architecture_review_service()
    asyncio.run(architecture_driver.run_architecture_review(
        story_id=STORY, repo_root=settings.repo_root, run_service=service, customer_id="vdb", initiated_by="u-hendro"))
    run = service.get(STORY)
    assert run.stage == "failed" and "Functional Agent" in run.error


def _prepare(company_id, roles):
    from jde_api_service.ai import connection, packs

    conn = connection.resolve_for_run(company_id)
    return conn, {role: packs.snapshot(conn.company_id, role) for role in roles}
