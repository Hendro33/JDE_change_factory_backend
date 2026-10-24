"""Corrections from the 23 Oct E2E audit (JADE audit findings F00-F09).

Each test reproduces the audited defect through the legitimate endpoints and
agent tools, then pins the corrected behaviour.
"""

from __future__ import annotations

import asyncio
import json

import claude_agent_sdk as sdk

from .conftest import headers


def _lc(client, story: str, customer: str = "vdb") -> dict:
    return client.get(f"/changes/{story}", headers=headers(customer)).json()["lifecycle"]


def _no_change_design(client, monkeypatch, story: str, *, sequence: list[str], human: list[str] | None = None,
                      architect_closes: bool = True) -> None:
    """A scripted Architect run (model boundary) that concludes no JD Edwards
    change is needed and -- as the real Architect does -- calls its own
    resolve_without_change tool during the run."""
    from jde_api_service.ai.project_tools import ProjectTools
    from jde_api_service.config import settings
    from jde_api_service.services import architecture_driver
    from jde_api_service.services.registry import get_architecture_review_service

    summary = {
        "architect_decision": {"recommended_route": "Resolve without Change", "confidence": 0.8,
                               "existing_functionality_found": "Standard inquiry P41202 already shows availability "
                                                               "per branch",
                               "alternatives_considered": [], "objects_affected": ["P41202"],
                               "dependencies_and_conflicts": [], "rollback_strategy": "nothing is changed"},
        "implementation_spec": {"sequence": sequence, "required_mcp_operations": [],
                                "human_actions_required": human or [],
                                "validation_approach": "walk through the guide with two order desk clerks"},
        "evidence": {},
    }

    async def fake_query(prompt, options):
        if architect_closes:
            tools = ProjectTools(company_id="vdb", story_id=story)
            tools.resolve_without_change({"story_id": story, "resolution_note": summary["architect_decision"][
                "existing_functionality_found"]})
        yield sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=2,
                                session_id="t", result="```json\n" + json.dumps(summary) + "\n```")

    monkeypatch.setattr(sdk, "query", fake_query)
    service = get_architecture_review_service()
    asyncio.run(architecture_driver.run_architecture_review(story_id=story, repo_root=settings.repo_root,
                                                            run_service=service, customer_id="vdb",
                                                            initiated_by="u-hendro"))
    assert service.get(story).stage == "done", service.get(story).error


# ---------------------------------------------------------------------
# F00 -- no JD Edwards change is a route, not a delivered outcome
# ---------------------------------------------------------------------
def test_f00_a_no_change_finding_with_work_left_is_not_done(client, monkeypatch):
    from .test_stage1_execution_safeguards import _approved_story

    s = "S-AUD-GUIDE"
    _approved_story(s)
    _no_change_design(client, monkeypatch, s, sequence=["Draft the availability guide from the P41202 screens",
                                                        "Publish the guide on the order desk intranet page"])
    lc = _lc(client, s)
    # Reproduced on DEV (CR-011be0ca): Done / Resolved without change while the guide was still to be written.
    assert lc["phase"] != "done", lc["nextAction"]
    assert lc["nextAction"]["action"] == "confirm_no_change" and lc["nextAction"]["owner"] == "product_manager"
    change = client.get(f"/changes/{s}", headers=headers("vdb")).json()
    assert change["state"] == "APPROVED"
    # The Application Manager confirms the finding: the guide work is handed over, the story stays open.
    r = client.post(f"/changes/{s}/resolve-without-change", headers=headers("vdb"), json={"note": "Agreed."})
    assert r.status_code == 200, r.text
    lc = r.json()["lifecycle"]
    assert lc["phase"] == "delivery" and lc["nextAction"]["action"] == "record_handoff"
    items = [w for w in lc["workItems"] if w["group"] == "handoff"]
    assert [w["title"] for w in items] == ["Draft the availability guide from the P41202 screens",
                                           "Publish the guide on the order desk intranet page"]
    # Acceptance is refused while the work is not done and checked.
    r = client.post(f"/changes/{s}/acceptance", headers=headers("vdb"),
                    json={"decision": "accepted", "note": "fine", "onBehalfOf": "Ellen Vos"})
    assert r.status_code in (409, 422), r.text
    for w in items:
        r = client.post(f"/changes/{s}/handoff/items/{w['id']}/complete", headers=headers("vdb"),
                        json={"implementer": "Order desk lead", "evidenceReference": "intranet page DOC-114",
                              "note": "written and published"})
        assert r.status_code == 200, r.text
    lc = _lc(client, s)
    assert lc["nextAction"]["action"] == "record_handoff_verification" and lc["phase"] != "done"
    r = client.post(f"/changes/{s}/handoff/verification", headers=headers("vdb"),
                    json={"passed": True, "evidenceReference": "walkthrough notes 23 Oct",
                          "note": "two clerks found availability per branch with the guide"})
    assert r.status_code == 200, r.text
    lc = _lc(client, s)
    assert lc["nextAction"]["action"] == "accept_outcome" and lc["nextAction"]["owner"] == "domain_owner"
    r = client.post(f"/changes/{s}/acceptance", headers=headers("vdb"),
                    json={"decision": "accepted", "note": "the guide answers it", "onBehalfOf": "Ellen Vos"})
    assert r.status_code == 200, r.text
    change = r.json()
    assert change["state"] == "RESOLVED_WITHOUT_CHANGE"
    assert change["lifecycle"]["phase"] == "done" and change["lifecycle"]["completionGaps"] == []


def test_f00_a_pure_no_change_finding_still_needs_the_business(client, monkeypatch):
    from .test_stage1_execution_safeguards import _approved_story

    s = "S-AUD-NOCHG"
    _approved_story(s)
    _no_change_design(client, monkeypatch, s, sequence=["No JD Edwards change is needed"])
    r = client.post(f"/changes/{s}/resolve-without-change", headers=headers("vdb"), json={"note": ""})
    lc = r.json()["lifecycle"]
    assert lc["phase"] == "validation" and lc["nextAction"]["action"] == "accept_outcome"
    assert [w["group"] for w in lc["workItems"]] == ["acceptance"]
    # A second confirmation is refused; nothing closed yet.
    assert client.post(f"/changes/{s}/resolve-without-change", headers=headers("vdb"), json={"note": ""}).status_code == 409
    r = client.post(f"/changes/{s}/acceptance", headers=headers("vdb"),
                    json={"decision": "accepted", "note": "P41202 does it", "onBehalfOf": "Ellen Vos"})
    assert r.status_code == 200 and r.json()["state"] == "RESOLVED_WITHOUT_CHANGE"


def test_f00_an_earlier_closure_states_its_gaps_and_can_be_reopened_deliberately(client, monkeypatch):
    """A story the old workflow closed on the Architect's finding alone keeps
    its record; the missing work and acceptance are stated, nothing is
    invented, and only an explicit decision reopens it."""
    from jde_mcp_server import backlog

    from .test_stage1_execution_safeguards import _approved_story

    s = "S-AUD-OLD"
    _approved_story(s)
    _no_change_design(client, monkeypatch, s, sequence=["Write the guide", "Publish the guide"])
    backlog.close_resolved(s, "closed by the earlier workflow", "architect")   # what the old tool did
    lc = _lc(client, s)
    assert lc["phase"] == "done" and lc["outcome"] == "resolved_without_change"
    assert any("never recorded as done" in g for g in lc["completionGaps"])
    assert any("No business acceptance is recorded" in g for g in lc["completionGaps"])
    assert client.post(f"/changes/{s}/reopen-resolved", headers=headers("vdb"), json={"reason": ""}).status_code == 422
    r = client.post(f"/changes/{s}/reopen-resolved", headers=headers("vdb"),
                    json={"reason": "the guide was never written"})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "APPROVED" and r.json()["lifecycle"]["nextAction"]["action"] == "confirm_no_change"
    record = backlog._load(s)
    assert record["closure_history"][0]["earlier_resolution"]["resolution_note"] == "closed by the earlier workflow"


def test_f00_affected_records_are_listed_for_the_application_manager(client, monkeypatch):
    from jde_mcp_server import backlog

    from .test_stage1_execution_safeguards import _approved_story

    _approved_story("S-AUD-L1")
    _no_change_design(client, monkeypatch, "S-AUD-L1", sequence=["Write the guide"])
    backlog.close_resolved("S-AUD-L1", "old closure", "architect")
    rows = client.get("/completion-gaps", headers=headers("vdb")).json()
    assert [r["id"] for r in rows] == ["S-AUD-L1"] and rows[0]["gaps"]


# ---------------------------------------------------------------------
# F02 -- status, prerequisites and actions agree
# ---------------------------------------------------------------------
def test_f02_an_expired_approval_offers_no_recording_control(client):
    """HW-007 on DEV: blocked with re-authorisation required, yet the Delivery
    tab still offered "Record applied in DEV". The controls now come from the
    same gate the endpoint uses, and the endpoint refuses."""
    import time

    from jde_mcp_server import approval

    from .test_recorded_delivery import _applied, _change as approved_change

    rec = approved_change(client, "S-AUD-EXP")
    lc = _lc(client, "S-AUD-EXP")
    assert "record_applied" in lc["allowedActions"]
    record = approval._load(rec["change_id"])  # noqa: SLF001
    record["expires_at"] = time.time() - 1
    approval._save(rec["change_id"], record)  # noqa: SLF001
    lc = _lc(client, "S-AUD-EXP")
    assert lc["nextAction"]["action"] == "approve_exact_change" and lc["health"] == "blocked"
    assert "record_applied" not in lc["allowedActions"]
    assert "expired" in lc["actionBlockers"]["record_applied"].lower()
    r = _applied(client, "S-AUD-EXP")
    assert r.status_code == 409, r.text
    # My Work and notifications follow the same next action: nobody is asked to record.
    work = client.get("/work", headers=headers("vdb")).json()
    rows = [c for key in ("needsYou", "waitingOnOthers", "jadeWorking") for c in work.get(key, [])
            if c["id"] == "S-AUD-EXP"]
    assert rows and rows[0]["lifecycle"]["nextAction"]["action"] == "approve_exact_change"


def test_f02_a_blocked_technical_outcome_is_not_a_fresh_start(client, monkeypatch):
    """HW-013 on DEV: the Technical Agent run completed but reported it was
    blocked for missing evidence; the next action still said "Start the
    implementation". Now: blocked, owned, and no restart until something it
    needs changes -- and the endpoint refuses a blind restart too."""
    from jde_api_service.technical import store as tstore

    from ._technical import ready_story

    s = "S-AUD-TBLK"
    ready_story(client, monkeypatch, s)
    a = client.get(f"/changes/{s}", headers=headers("vdb")).json()
    lc = a["lifecycle"]
    assert lc["nextAction"]["action"] == "start_technical_prepare"
    from jde_api_service.technical import service as tservice

    asg = tservice.assignment("vdb", s)
    run = tstore.start_run(company_id="vdb", story_id=s, purpose="prepare", design_revision=asg["design_revision"],
                           baseline_id=asg["design_approval"]["baseline_id"],
                           design_approval_id=asg["design_approval"]["id"], initiated_by="u-hendro")
    tstore.finish_run(run["run_id"], status="completed",
                      outcome={"kind": "blocked_missing_evidence",
                               "explanation": "No source export of R5541001 is available. Upload the text export "
                                              "so the change can be prepared.", "questions": []})
    lc = _lc(client, s)
    assert lc["health"] == "blocked" and lc["nextAction"]["action"] == "provide_technical_evidence"
    assert lc["nextAction"]["owner"] == "admin" and "R5541001" in lc["nextAction"]["summary"]
    assert "start_technical_prepare" not in lc["allowedActions"]
    assert lc["actionBlockers"]["start_technical_prepare"]
    r = client.post(f"/changes/{s}/technical/runs", headers=headers("vdb"), json={"purpose": "prepare"})
    assert r.status_code == 409 and "nothing it needs has changed" in r.text


# ---------------------------------------------------------------------
# Backlog Review -- the gate stays; several stories are sent or held at once
# ---------------------------------------------------------------------
def _am_ready(client, sid: str) -> str:
    from .test_concurrency_and_stale_authority import _story_in_review

    _story_in_review(client, sid)
    assert client.post(f"/changes/{sid}/domain-review/approve", headers=headers("bwm"), json={"note": "ok"}).status_code == 200
    return sid


def test_backlog_review_sends_several_stories_through_the_same_gate(client, monkeypatch):
    from jde_mcp_server import backlog

    from jde_api_service.routers import domain_governance

    started: list[str] = []

    async def no_architect(**kw):
        started.append(kw["story_id"])

    monkeypatch.setattr(domain_governance, "run_architecture_review", no_architect)
    a, b = _am_ready(client, "S-BL-A"), _am_ready(client, "S-BL-B")
    backlog.propose_to_backlog("S-BL-C", "As a clerk I want Y", {}, "Low", source="Business")   # not reviewed yet
    r = client.post("/backlog/send-to-architecture", headers=headers("bwm"),
                    json={"changeIds": [a, b, "S-BL-C"], "note": "next sprint"})
    assert r.status_code == 200, r.text
    res = {x["changeId"]: x for x in r.json()}
    assert res[a]["sent"] and res[b]["sent"] and not res["S-BL-C"]["sent"] and res["S-BL-C"]["detail"]
    assert started == [a, b]
    for sid in (a, b):
        backlog.require_approved(sid)   # Gate 1 recorded exactly as the single action does
        assert _lc(client, sid, "bwm")["phase"] != "story_review"


def test_backlog_review_holds_a_story_without_nagging_and_releases_it(client, monkeypatch):
    from jde_api_service.routers import domain_governance

    async def no_architect(**kw):
        return None

    monkeypatch.setattr(domain_governance, "run_architecture_review", no_architect)
    sid = _am_ready(client, "S-BL-H")
    assert client.post(f"/changes/{sid}/domain-review/hold", headers=headers("bwm"), json={"reason": ""}).status_code == 422
    r = client.post(f"/changes/{sid}/domain-review/hold", headers=headers("bwm"),
                    json={"reason": "waits for the price list project"})
    assert r.status_code == 200, r.text
    change = client.get(f"/changes/{sid}", headers=headers("bwm")).json()
    assert change["backlogHold"]["reason"] == "waits for the price list project"
    assert "On hold" in change["lifecycle"]["nextAction"]["summary"]
    work = client.get("/work", headers=headers("bwm")).json()
    assert sid not in [c["id"] for k in ("needsYou", "waitingOnOthers") for c in work[k]]
    # Sending it releases the hold, on the record.
    r = client.post("/backlog/send-to-architecture", headers=headers("bwm"), json={"changeIds": [sid]})
    assert r.json()[0]["sent"], r.text
    from jde_api_service.services.registry import get_domain_review_service

    review = get_domain_review_service().get(sid)
    assert review.hold is None and review.hold_history[0]["release_note"] == "sent to architecture"


def test_backlog_capacity_reports_measured_cost_only(client):
    r = client.get("/backlog/capacity", headers=headers("bwm"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["architectRunsMeasured"] == 0 and body["averageArchitectRunUsd"] is None
    assert "inSolutioning" in body and "monthSpendUsd" in body


def test_f02_the_test_step_follows_its_own_gate(client):
    """HW-006 in the preview: applied in DEV, the approval then expired; the
    next action still said "Test and record" while recording was refused."""
    import time

    from jde_mcp_server import approval

    from .test_recorded_delivery import _applied, _change as approved_change
    from .fixtures.fake_ais import apply_in_dev

    rec = approved_change(client, "S-AUD-TST")
    apply_in_dev("vdb", "P4210", "CIQ0001", "PDOCTYPE", "SO")
    assert _applied(client, "S-AUD-TST", note="set in DEV").status_code == 200
    lc = _lc(client, "S-AUD-TST")
    assert lc["nextAction"]["action"] == "run_or_record_test" and "record_test" in lc["allowedActions"]
    record = approval._load(rec["change_id"])  # noqa: SLF001
    record["expires_at"] = time.time() - 1
    approval._save(rec["change_id"], record)  # noqa: SLF001
    lc = _lc(client, "S-AUD-TST")
    assert lc["health"] == "blocked" and lc["nextAction"]["action"] == "approve_exact_change", lc["nextAction"]
    assert "record_test" not in lc["allowedActions"] and lc["actionBlockers"]["record_test"]
    r = client.post("/changes/S-AUD-TST/delivery/test-result", headers=headers("vdb"),
                    json={"passed": True, "evidenceReference": "TEST-1", "note": "order entered"})
    assert r.status_code == 409


def test_f07_a_story_finished_under_the_earlier_workflow_states_what_was_not_recorded(client):
    """HW-001 on DEV: Done/Delivered, while the outcome asked for an acceptance
    nobody could give. The recorded facts stay; the missing checkpoints are
    stated; nothing is asked and nothing is manufactured."""
    from jde_mcp_server import backlog

    from .test_recorded_delivery import _applied, _change as approved_change
    from .fixtures.fake_ais import apply_in_dev

    approved_change(client, "S-AUD-HIST")
    apply_in_dev("vdb", "P4210", "CIQ0001", "PDOCTYPE", "SO")
    assert _applied(client, "S-AUD-HIST", note="set in DEV").status_code == 200
    rec = backlog._load("S-AUD-HIST")  # noqa: SLF001 -- a record closed by the earlier workflow (fixture)
    rec["state"] = "CLOSED"
    backlog._save("S-AUD-HIST", rec)  # noqa: SLF001
    change = client.get("/changes/S-AUD-HIST", headers=headers("vdb")).json()
    lc = change["lifecycle"]
    assert lc["phase"] == "done" and lc["outcome"] == "delivered"
    assert any(g.startswith("Accepted by the business: not recorded") for g in lc["completionGaps"]), lc["completionGaps"]
    card = lc["outcomeCard"]
    assert card["businessAccepted"]["done"] is False and "Nothing is asked" in card["whatToDoNow"]
    assert not lc["nextAction"]["action"]
    assert backlog._load("S-AUD-HIST")["state"] == "CLOSED"  # noqa: SLF001 -- reading changed nothing
    # Repair brief P2: no impossible retrospective task -- the acceptance step says "not recorded", not "to do".
    assert not [w for w in lc["workItems"] if w["state"] in ("todo", "in_progress", "blocked")], lc["workItems"]
    from jde_api_service.models.delivery_view import WorkItem
    from jde_api_service.services.work_items import settle_finished

    settled = settle_finished([WorkItem(id="ACCEPT", title="Accept the outcome", owner_kind="business", who="Domain Owner",
                                        state="todo", group="acceptance"),
                               WorkItem(id="BUILD", title="Build", owner_kind="agent", who="JADE", state="done")])
    assert [w.state for w in settled] == ["not_recorded", "done"]
