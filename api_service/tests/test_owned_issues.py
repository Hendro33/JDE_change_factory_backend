"""
The shared information model (usability redesign, step 1): structured agent
results, owned issues, a blocker-aware next action, "What needs to happen",
the Administrator's setup list, the writing style every agent receives and
the full agent output kept for Administrators.

Nothing here may approve, refuse or execute anything: the tests also pin that
the delivery gate is only asked, and that a blocker never hides the story's
state.
"""

from __future__ import annotations

import asyncio
import json
import time

import claude_agent_sdk as sdk
import pytest

from jde_api_service.models.change import ApprovalRecord, ArchitectDecision, Change, ExactChange, ExecutionStatus
from jde_api_service.models.issues import AgentResult, ResultIssue
from jde_api_service.services import issues, lifecycle

from .conftest import headers


# ---------------------------------------------------------------------
# Unit level: the lifecycle with stated facts
# ---------------------------------------------------------------------
def _change(**kw) -> Change:
    base = dict(id="S-1", customer_id="c1", title="Freight line type", source="Business", state="APPROVED",
                created_at="2026-10-01T00:00:00+00:00", updated_at="2026-10-01T00:00:00+00:00",
                architecture_review_stage="done")
    base.update(kw)
    return Change(**base)


def _decision(result: AgentResult | None = None, route: str = "Functional Agent", **kw) -> ArchitectDecision:
    return ArchitectDecision(recommended_route=route, confidence=0.7, decided_at="2026-10-01T00:00:00+00:00",
                             result=result, **kw)


def _proposed(approved: bool = False) -> dict:
    return dict(exact_change=ExactChange(tool="set_processing_option", application="P4210", version="V1", option="1",
                                         execution=ExecutionStatus()),
                change_approval=ApprovalRecord(approval_id="C1", kind="change",
                                               status="approved" if approved else "pending"))


@pytest.fixture()
def facts(monkeypatch):
    """Each unit test states its facts; nothing is read from the database."""
    monkeypatch.setattr(lifecycle, "_design_facts", lambda c, s: {"baseline": {"status": "current"}, "design_approved": False})
    monkeypatch.setattr(lifecycle, "_technical_facts", lambda c, s: {"package": None, "record": None, "running": False})
    monkeypatch.setattr(lifecycle, "_asbuilt_final", lambda c, s: None)
    monkeypatch.setattr(lifecycle, "_process_decided", lambda c, s: None)
    state = {"baseline": None, "gate": [], "resolutions": {}}
    monkeypatch.setattr(issues, "_baseline", lambda c, s: state["baseline"])
    monkeypatch.setattr(issues, "delivery_gate_issues", lambda c, lc: list(state["gate"]))
    monkeypatch.setattr(issues, "resolutions", lambda c, s: dict(state["resolutions"]))
    return state


FINANCE = ResultIssue(title="Which revenue account should freight lines post to?", consequence="Freight cannot be "
                      "invoiced correctly", owner="business", who="Finance", action="Name the GL account",
                      blocks=True, proposed_answer="Use object account 5110, as for service charges")


def test_a_blocking_business_question_replaces_authorise_with_waiting_for(facts):
    result = AgentResult(conclusion="Standard line type F exists and fits; only the revenue account is open.",
                         issues=[FINANCE, ResultIssue(title="Line type F may need a new description", owner="decision",
                                                      blocks=False)])
    lc = lifecycle.derive(_change(architect_decision=_decision(result), **_proposed()))
    assert lc.phase == "solution_review" and lc.health == "blocked"
    na = lc.next_action
    assert na.summary.startswith("Waiting for Finance:") and na.owner == "domain_owner"
    assert na.action == "answer_issue" and na.action != "approve_exact_change"
    # Every open point has an owner; the blocking one leads "What needs to happen".
    assert {i.owner_label for i in lc.issues} == {"Finance", "Application Manager"}
    assert lc.what_next[0].primary and lc.what_next[0].blocking
    assert len(lc.what_next) <= issues.MAX_WORK_LINES


def test_non_blocking_points_leave_the_decision_with_the_application_manager(facts):
    result = AgentResult(conclusion="ok", issues=[ResultIssue(title="Consider renaming", owner="decision")])
    lc = lifecycle.derive(_change(architect_decision=_decision(result), **_proposed()))
    assert lc.next_action.action == "approve_exact_change" and lc.health == "waiting_decision"
    assert lc.issues[0].blocks is False


def test_a_blocking_design_choice_is_a_decision_for_the_application_manager(facts):
    choice = ResultIssue(title="New line type code: FR or F2?", owner="decision", blocks=True,
                         proposed_answer="FR, free in UDC 00/LT")
    lc = lifecycle.derive(_change(architect_decision=_decision(AgentResult(issues=[choice])), **_proposed()))
    assert lc.next_action.kind == "decision" and lc.next_action.owner == "product_manager"
    assert lc.next_action.summary.startswith("Decide before authorising")


def test_jade_owned_problems_never_block_or_ask_anyone(facts):
    own = ResultIssue(title="F40205 description read was refused; retried with DLDL01", owner="jade", blocks=True)
    lc = lifecycle.derive(_change(architect_decision=_decision(AgentResult(issues=[own])), **_proposed()))
    assert lc.next_action.action == "approve_exact_change"
    assert lc.issues[0].owner == "jade" and lc.issues[0].blocks is False


def test_answers_clear_the_blocker_and_ask_for_reassessment(facts):
    result = AgentResult(issues=[FINANCE])
    change = _change(architect_decision=_decision(result), **_proposed())
    first = lifecycle.derive(change)
    iss = first.issues[0]
    facts["resolutions"] = {iss.id: {"issue_id": iss.id, "issue_title": iss.title, "status": "answered",
                                     "answer": "5110", "by": "Ellen", "at": "2026-10-02T00:00:00+00:00"}}
    lc = lifecycle.derive(change)
    assert lc.issues[0].resolution is not None and lc.issues[0].resolution.answer == "5110"
    assert lc.next_action.action == "rerun_solutioning"


def test_older_designs_get_their_evidence_gaps_sorted_by_owner(facts):
    facts["baseline"] = {"manifest": {"gaps": [
        {"kind": "missing", "description": "table_browse on F40205 was not read: operator 'like' refused",
         "question": "No action for people: Jade's own read could not be completed; the next solution run reads it "
                     "again.", "blocked_step": "", "source": "system", "owner": "jade"},
        {"kind": "missing", "description": "No approved test version", "question": "Can an Admin add an approved "
         "test version for P4210?", "blocked_step": "", "source": "architect"},
        {"kind": "missing", "description": "The GL class for freight is unknown",
         "question": "Which GL class and AAI should freight lines use?", "blocked_step": "setting the line type's GL "
         "class", "source": "architect"},
    ], "confidence_limitations": ["Live AIS response shapes are unverified"]}}
    lc = lifecycle.derive(_change(architect_decision=_decision(), **_proposed()))
    kinds = {i.kind: i for i in lc.issues}
    assert set(kinds) == {"jade", "setup", "business"}
    assert kinds["jade"].blocks is False and kinds["setup"].owner == "admin"
    assert kinds["business"].owner_label == "Finance" and kinds["business"].blocks
    assert lc.next_action.summary.startswith("Waiting for Finance")
    derived = issues.result_for(_change(architect_decision=_decision(), **_proposed()), facts["baseline"])
    assert derived.derived and derived.conclusion.startswith("JADE recommends a configuration change")


def test_a_delivery_gate_refusal_replaces_record_applied(facts):
    facts["gate"] = [issues._gate_issue("this company's scope does not confirm DEV isolation. An environment ...")]
    lc = lifecycle.derive(_change(architect_decision=_decision(), **_proposed(approved=True)))
    assert lc.health == "blocked" and lc.next_action.action == "admin_setup" and lc.next_action.owner == "admin"
    assert "DEV isolation" in lc.next_action.summary
    expired = issues._gate_issue("change C1 approval expired at 1700000000 -- propose it again")
    assert (expired.owner, expired.action_id) == ("product_manager", "approve_exact_change")


def test_jade_at_work_is_never_overridden(facts):
    lc = lifecycle.derive(_change(architecture_review_stage="analyzing", architect_decision=_decision(
        AgentResult(issues=[FINANCE]))))
    assert lc.next_action.owner == "jade"


def test_the_setup_list_has_one_task_per_gap_with_every_affected_story(facts):
    setup = ResultIssue(title="DEV isolation is not confirmed", owner="administrator", blocks=True)
    a = _change(id="S-A", architect_decision=_decision(AgentResult(issues=[setup])), **_proposed())
    b = _change(id="S-B", architect_decision=_decision(AgentResult(issues=[setup])), **_proposed())
    for c in (a, b):
        c.lifecycle = lifecycle.derive(c)
    tasks = issues.setup_list([a, b])
    assert len(tasks) == 1 and [s["id"] for s in tasks[0]["stories"]] == ["S-A", "S-B"] and tasks[0]["blocks"]


def test_results_are_read_leniently_and_never_cut():
    long = "x" * 5000
    r = issues.parse_result({"conclusion": long, "changes": ["plain text change", {"what": "Line type F", "current": "",
                             "proposed": "GL class FR01", "status": "bogus"}], "issues": ["a plain issue"],
                             "uncertainty": ["", "AIS shape unverified"]})
    assert r.conclusion == long
    assert [c.status for c in r.changes] == ["proposed", "proposed"]
    assert r.issues[0].title == "a plain issue" and r.uncertainty == ["AIS shape unverified"]
    assert issues.parse_result(None) is None


def test_an_answer_without_a_valid_route_is_incomplete_not_human_implementation():
    from jde_api_service.services import architecture_driver

    with pytest.raises(architecture_driver.IncompleteResult):
        architecture_driver._architect_decision_from_summary({"architect_decision": {"recommended_route": "Maybe"}})


def test_every_agent_gets_the_writing_style():
    from jde_api_service.ai import packs

    snap = packs.PackSnapshot.__new__(packs.PackSnapshot)
    object.__setattr__(snap, "content", {"instructions": "Do the work.", "skills": [], "inputs": "", "outputs": ""})
    prompt = packs.PackSnapshot.prompt(snap)
    assert prompt.endswith(packs.WRITING_POLICY)
    assert "Conclusion first" in prompt


# ---------------------------------------------------------------------
# End to end through the API
# ---------------------------------------------------------------------
def _scripted_architect(monkeypatch, story: str, summary: dict) -> None:
    from jde_api_service.config import settings
    from jde_api_service.services import architecture_driver
    from jde_api_service.services.registry import get_architecture_review_service

    seen = {}

    async def fake_query(prompt, options):
        seen["prompt"] = prompt
        yield sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=2,
                                session_id="t", result="```json\n" + json.dumps(summary) + "\n```")

    monkeypatch.setattr(sdk, "query", fake_query)
    asyncio.run(architecture_driver.run_architecture_review(
        story_id=story, repo_root=settings.repo_root, run_service=get_architecture_review_service(),
        customer_id="vdb"))
    return seen


def _summary(issue_blocks: bool = True) -> dict:
    return {
        "architect_decision": {"recommended_route": "Functional Agent", "confidence": 0.7,
                               "existing_functionality_found": "Line type F exists and is in use.",
                               "alternatives_considered": [], "objects_affected": ["F40205"],
                               "dependencies_and_conflicts": [], "rollback_strategy": "set the value back"},
        "implementation_spec": {"sequence": ["set the value"], "required_mcp_operations": [],
                                "human_actions_required": [], "validation_approach": "order entry test"},
        "evidence": {},
        "result": {"conclusion": "Standard line type F fits; Finance must name the revenue account.",
                   "changes": [{"what": "P4210 version CIQ0001, option PDOCTYPE", "current": "S3",
                                "proposed": "SO", "status": "verified"}],
                   "issues": [{"title": "Which revenue account should freight post to?", "owner": "business",
                               "who": "Finance", "consequence": "Freight is not invoiced correctly",
                               "action": "Name the account", "blocks": issue_blocks,
                               "proposed_answer": "5110"}],
                   "uncertainty": ["The GL class mapping was not readable"]},
    }


def test_architect_result_owned_issue_answer_and_retained_output(client, monkeypatch):
    from jde_mcp_server import approval

    from ._discovery import ready_company
    from .test_stage1_execution_safeguards import OPERATION, _approved_story, _full_scope, _save_scope

    ready_company(client, "vdb")
    _save_scope(client, "vdb", _full_scope())
    _approved_story("S-IS-1")
    _scripted_architect(monkeypatch, "S-IS-1", _summary())
    approval.propose_change("S-IS-1", {**OPERATION, "story_id": "S-IS-1"}, "processing_option_update")

    change = client.get("/changes/S-IS-1", headers=headers("vdb")).json()
    result = change["architectDecision"]["result"]
    assert result["conclusion"].startswith("Standard line type F fits") and result["changes"][0]["status"] == "verified"
    lc = change["lifecycle"]
    assert lc["nextAction"]["summary"].startswith("Waiting for Finance") and lc["health"] == "blocked"
    issue = next(i for i in lc["issues"] if i["ownerLabel"] == "Finance")
    assert issue["blocks"] and issue["proposedAnswer"] == "5110"

    # An answer needs text; a recorded one clears the blocker and asks for reassessment.
    url = f"/changes/S-IS-1/issues/{issue['id']}/resolution"
    assert client.post(url, headers=headers("vdb"), json={"status": "answered", "answer": " "}).status_code == 422
    r = client.post(url, headers=headers("vdb"), json={"status": "answered", "answer": "Object account 5110"})
    assert r.status_code == 200, r.text
    lc = r.json()["lifecycle"]
    assert lc["nextAction"]["action"] == "rerun_solutioning"
    answered = next(i for i in lc["issues"] if i["id"] == issue["id"])
    assert answered["resolution"]["answer"] == "Object account 5110" and answered["resolution"]["by"]

    # The next Architect run is given the answer as a statement to reassess with.
    seen = _scripted_architect(monkeypatch, "S-IS-1", _summary(issue_blocks=False))
    assert "Object account 5110" in seen["prompt"] and "not instructions" in seen["prompt"]

    # The Architect's full answer is kept, exactly, for Administrators.
    runs = client.get("/admin/ai/runs", headers=headers("vdb")).json()
    run = next(r for r in runs if r["story_id"] == "S-IS-1")
    assert "output" not in run and run["output_chars"] > 0
    out = client.get(f"/admin/ai/runs/{run['run_id']}/output", headers=headers("vdb")).json()
    assert "Finance must name the revenue account" in out["output"]


def test_issue_answers_are_refused_for_points_jade_or_the_gate_own(client, monkeypatch):
    from .test_recorded_delivery import _change as approved_change
    from .test_stage1_execution_safeguards import _full_scope, _save_scope

    approved_change(client, "S-IS-GATE")
    assert client.get("/changes/S-IS-GATE", headers=headers("vdb")).json()["lifecycle"]["nextAction"]["action"] \
        == "record_applied"

    # DEV isolation withdrawn after approval: the next action is the Administrator's, not "record applied".
    scope = _full_scope()
    scope["environment"]["isolationConfirmed"] = False
    _save_scope(client, "vdb", scope)
    lc = client.get("/changes/S-IS-GATE", headers=headers("vdb")).json()["lifecycle"]
    assert lc["health"] == "blocked" and lc["nextAction"]["owner"] == "admin"
    assert lc["nextAction"]["action"] == "admin_setup" and "DEV isolation" in lc["nextAction"]["summary"]
    gate = next(i for i in lc["issues"] if i["source"] == "delivery_gate")
    r = client.post(f"/changes/S-IS-GATE/issues/{gate['id']}/resolution", headers=headers("vdb"),
                    json={"status": "resolved"})
    assert r.status_code == 409
    tasks = client.get("/setup-list", headers=headers("vdb")).json()
    assert any(t["title"] == gate["title"] and "S-IS-GATE" in [s["id"] for s in t["stories"]] for t in tasks)

    # Fixed in Administration: delivery continues where it was.
    _save_scope(client, "vdb", _full_scope())
    assert client.get("/changes/S-IS-GATE", headers=headers("vdb")).json()["lifecycle"]["nextAction"]["action"] \
        == "record_applied"


def test_an_expired_approval_is_shown_before_any_delivery_step(client):
    from jde_mcp_server import approval

    from .test_recorded_delivery import _change as approved_change

    rec = approved_change(client, "S-IS-EXP")
    record = approval._load(rec["change_id"])  # noqa: SLF001
    record["expires_at"] = time.time() - 1
    approval._save(rec["change_id"], record)  # noqa: SLF001
    na = client.get("/changes/S-IS-EXP", headers=headers("vdb")).json()["lifecycle"]["nextAction"]
    assert na["owner"] == "product_manager" and na["action"] == "approve_exact_change"
    assert "expired" in na["summary"]


def test_every_view_reports_the_same_state(client):
    """One change has one state: the story, the list, My Work, the delivery
    queue and the counts all read the same canonical lifecycle (COH-001)."""
    from jde_mcp_server import approval

    from .fixtures.fake_ais import apply_in_dev
    from .test_recorded_delivery import _applied, _change as approved_change
    from .test_stage1_execution_safeguards import OPERATION, _approved_story

    approved_change(client, "S-CON-APPLY")                       # delivery: record applied
    approved_change(client, "S-CON-TEST")
    apply_in_dev("vdb", "P4210", "CIQ0001", "PDOCTYPE", "SO")
    assert _applied(client, "S-CON-TEST").status_code == 200    # validation
    _approved_story("S-CON-REVIEW")
    approval.propose_change("S-CON-REVIEW", {**OPERATION, "story_id": "S-CON-REVIEW"}, "processing_option_update")

    h = headers("vdb")
    story = {sid: client.get(f"/changes/{sid}", headers=h).json()["lifecycle"]
             for sid in ("S-CON-APPLY", "S-CON-TEST", "S-CON-REVIEW")}
    listed = {c["id"]: c["lifecycle"] for c in client.get("/changes", headers=h).json() if c["id"] in story}
    work = client.get("/work", headers=h).json()
    in_work = {c["id"]: c["lifecycle"] for bucket in ("needsYou", "waitingOnOthers", "jadeWorking")
               for c in work[bucket] if c["id"] in story}
    queue = {e["changeId"]: e for e in client.get("/delivery-queue", headers=h).json() if e["changeId"] in story}

    def key(lc):
        return (lc["phase"], lc["health"], lc["nextAction"]["action"], lc["nextAction"]["summary"])

    for sid, lc in story.items():
        assert key(listed[sid]) == key(lc), sid
        assert key(in_work[sid]) == key(lc), sid
        if sid in queue:
            e = queue[sid]
            assert (e["phase"], e["health"], e["nextAction"]) == (lc["phase"], lc["health"], lc["nextAction"]["summary"]), sid
    assert story["S-CON-APPLY"]["phase"] == "delivery" and story["S-CON-TEST"]["phase"] == "validation"

    totals = {t["key"]: t["value"] for t in client.get("/metrics", headers=h).json()["totals"]}
    phases = [lc["phase"] for lc in listed.values()]
    assert totals["in_delivery"] >= phases.count("delivery") and totals["awaiting_business_validation"] >= phases.count("validation")
    every = [c["lifecycle"]["phase"] for c in client.get("/changes", headers=h).json() if c.get("lifecycle")]
    assert totals["in_delivery"] == every.count("delivery")
    assert totals["awaiting_business_validation"] == every.count("validation")
