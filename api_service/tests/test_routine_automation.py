"""Redesign step 4: routine work runs by itself.

Intake starts by itself; a changed story, a changed setup or a passing
failure make solutioning run again by itself (answers do not: the
Application Manager re-runs the Architect) -- once per input, at
most MAX_RUNS times a day; approved changes are re-checked before delivery
and someone is told only when something changed; notifications follow the
lifecycle. The switch per customer turns all of it off.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from jde_api_service.models.issues import AgentResult, ResultIssue
from jde_api_service.services import automation, issues, lifecycle, notifications

from .conftest import headers
from .test_owned_issues import FINANCE, _change, _decision, _proposed, _scripted_architect, _summary, facts  # noqa: F401


ON = {"enabled": True, "jira_pickup": True, "setup_changed_at": None, "setup_changed": "",
      "last_jira_pickup_at": None, "updated_by": None, "updated_at": None}


@pytest.fixture()
def automation_on(monkeypatch):
    monkeypatch.setenv("JDE_ROUTINE_AUTOMATION", "true")
    monkeypatch.setenv("JDE_AUTOMATION_SETTLE_SECONDS", "0")
    started: list[tuple] = []
    monkeypatch.setattr(automation, "_launch", lambda fn, kw, **_: started.append((fn.__name__, kw)))
    return started


def _iso(minutes_ago: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def _no_baseline(monkeypatch):
    from jde_api_service.discovery import baseline

    monkeypatch.setattr(baseline, "current_for_story", lambda c, s: {"status": "current", "baseline_id": "B1"})


# ---------------------------------------------------------------------
# Deciding (unit level)
# ---------------------------------------------------------------------
def _answered(facts, change):
    iss = lifecycle.derive(change).issues[0]
    facts["resolutions"] = {iss.id: {"issue_id": iss.id, "issue_title": iss.title, "status": "answered",
                                     "answer": "5110", "by": "Ellen", "at": _iso()}}


def test_answers_never_start_a_run_the_manager_reruns(facts, monkeypatch, automation_on):
    """Hendro, 23 Oct evening: an answer to one of several points restarted the
    Architect. Answers now never start a run; the Application Manager re-runs."""
    _no_baseline(monkeypatch)
    monkeypatch.setattr(automation, "settings_for", lambda c: dict(ON))
    monkeypatch.setattr(automation, "state_of", lambda c, s: {})
    other = ResultIssue(title="Which branch plants?", owner="business", who="Sales", blocks=False)
    change = _change(architect_decision=_decision(AgentResult(issues=[FINANCE, other])), **_proposed())
    _answered(facts, change)
    lc = lifecycle.derive(change)
    assert lc.automation is None
    assert lc.next_action.action == "rerun_solutioning" and lc.next_action.owner == "product_manager"


def _setup_waiting(facts):
    setup = ResultIssue(title="No approved test version is set up in JADE for this customer", owner="setup",
                        blocks=True, action="Set it up in Administration")
    change = _change(architect_decision=_decision(AgentResult(issues=[setup])), **_proposed())
    base = issues.apply(change, lifecycle._solution_onwards(change, "c1", "S-1"), "c1")
    return change, base, {**ON, "setup_changed_at": _iso(), "setup_changed": "governance scope saved"}


def test_the_same_input_is_never_reassessed_twice_and_runs_are_capped(facts, monkeypatch, automation_on):
    _no_baseline(monkeypatch)
    change, base, later = _setup_waiting(facts)
    plan = automation.plan_for(change, base, "c1", state={}, settings=later)
    assert plan.state == "scheduled"
    # Already run for exactly this input: nothing is planned again.
    done = {"runs": [{"kind": "solutioning", "at": _iso(), "signature": plan.signature}]}
    assert automation.plan_for(change, base, "c1", state=done, settings=later) is None
    # Three automatic runs today: a person takes over, with the reason shown.
    busy = {"runs": [{"kind": "solutioning", "at": _iso(i), "signature": f"x{i}"} for i in range(3)]}
    capped = automation.plan_for(change, base, "c1", state=busy, settings=later)
    assert capped.state == "capped" and "by hand" in capped.label
    monkeypatch.setattr(automation, "settings_for", lambda c: later)
    monkeypatch.setattr(automation, "state_of", lambda c, s: busy)
    lc = lifecycle.derive(change)
    assert lc.next_action.owner != "jade" and capped.label in lc.open_items
    # Switched off for this customer: nothing is planned at all.
    assert automation.plan_for(change, base, "c1", state={}, settings={**later, "enabled": False}) is None


def test_a_setup_change_waits_to_settle(facts, monkeypatch, automation_on):
    _no_baseline(monkeypatch)
    monkeypatch.setenv("JDE_AUTOMATION_SETTLE_SECONDS", "600")
    change, base, later = _setup_waiting(facts)
    plan = automation.plan_for(change, base, "c1", state={}, settings=later)
    assert plan.state == "scheduled" and plan.due_at > _iso()


def test_a_setup_change_reassesses_a_design_waiting_on_setup(facts, monkeypatch, automation_on):
    _no_baseline(monkeypatch)
    setup = ResultIssue(title="No approved test version is set up in JADE for this customer", owner="setup", blocks=True,
                        action="Grant the read in Administration")
    change = _change(architect_decision=_decision(AgentResult(issues=[setup])), **_proposed())
    base = issues.apply(change, lifecycle._solution_onwards(change, "c1", "S-1"), "c1")
    assert any(i.kind == "setup" and i.blocks for i in base.issues)
    assert automation.plan_for(change, base, "c1", state={}, settings=dict(ON)) is None
    later = {**ON, "setup_changed_at": _iso(), "setup_changed": "governance scope saved"}
    plan = automation.plan_for(change, base, "c1", state={}, settings=later)
    assert plan.reason == "setup changed" and "governance scope saved" in plan.label


def test_a_changed_story_and_a_passing_failure_rerun_but_a_key_problem_does_not(facts, monkeypatch, automation_on):
    from jde_api_service.discovery import baseline

    change = _change(architect_decision=_decision(AgentResult()), **_proposed())
    monkeypatch.setattr(baseline, "current_for_story",
                        lambda c, s: {"status": "needs_reassessment", "baseline_id": "B1"})
    base = issues.apply(change, lifecycle._solution_onwards(change, "c1", "S-1"), "c1")
    assert automation.plan_for(change, base, "c1", state={}, settings=dict(ON)).reason == \
        "the story changed after the design"

    _no_baseline(monkeypatch)
    ran = {"runs": [{"kind": "solutioning", "at": _iso(30), "signature": "a"}]}
    failed = _change(architecture_review_stage="failed", architecture_review_error="The run timed out")
    lc = lifecycle._solution_onwards(failed, "c1", "S-1")
    plan = automation.plan_for(failed, lc, "c1", state=ran, settings=dict(ON))
    assert plan.reason == "retry after a failed run" and plan.state == "scheduled"
    # Only a run JADE itself started is retried; a person's failed run stays theirs.
    assert automation.plan_for(failed, lc, "c1", state={}, settings=dict(ON)) is None
    keyfail = _change(architecture_review_stage="failed", architecture_review_error="401 invalid x-api-key")
    lc = lifecycle._solution_onwards(keyfail, "c1", "S-1")
    assert automation.plan_for(keyfail, lc, "c1", state=ran, settings=dict(ON)) is None


def test_after_authorisation_answers_do_not_restart_solutioning(facts, monkeypatch, automation_on):
    _no_baseline(monkeypatch)
    change = _change(architect_decision=_decision(AgentResult(issues=[FINANCE])), **_proposed(approved=True))
    _answered(facts, change)
    lc = lifecycle._solution_onwards(change, "c1", "S-1")
    lc = issues.apply(change, lc, "c1")
    assert automation.plan_for(change, lc, "c1", state={}, settings=dict(ON)) is None


# ---------------------------------------------------------------------
# Intake through the API
# ---------------------------------------------------------------------
def _new_request(client, **extra):
    r = client.post("/change-requests", headers=headers("vdb"), json={
        "title": "Freight line", "businessSource": "Business", "rawContent": "Add a freight line type", **extra})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_a_new_request_becomes_a_story_by_itself(client, automation_on, monkeypatch):
    from jde_api_service.services import agent_settings

    monkeypatch.setattr(agent_settings, "require_enabled", lambda *a, **k: None)
    rid = _new_request(client)
    assert [n for n, _ in automation_on] == ["run_enhancement"]
    assert automation_on[0][1]["story_id"] == rid and automation_on[0][1]["customer_id"] == "vdb"
    state = automation.state_of("vdb", rid)
    assert state["intake_queued"] and state["runs"][0]["kind"] == "intake"
    # Started once: a second pass does not start it again.
    automation.sweep("vdb", recheck=False)
    assert len(automation_on) == 1
    log = client.get("/automation", headers=headers("vdb")).json()["events"]
    assert log[0]["storyId"] == rid and log[0]["action"] == "started"


def test_unticked_analyse_or_automation_off_leaves_the_request_for_a_person(client, automation_on, monkeypatch):
    from jde_api_service.services import agent_settings

    monkeypatch.setattr(agent_settings, "require_enabled", lambda *a, **k: None)
    rid = _new_request(client, startAnalysis=False)
    assert automation_on == []
    lc = client.get(f"/changes/{rid}", headers=headers("vdb")).json()["lifecycle"]
    assert lc["nextAction"]["action"] == "start_analysis" and lc["automation"] is None

    r = client.put("/automation", headers=headers("vdb"), json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    _new_request(client)
    assert automation_on == []
    assert client.get("/automation", headers=headers("vdb")).json()["events"][0]["kind"] == "settings"


def test_a_queued_request_waiting_for_a_free_slot_shows_jade(client, automation_on, monkeypatch):
    from jde_api_service.services import agent_settings

    monkeypatch.setattr(agent_settings, "require_enabled", lambda *a, **k: None)
    monkeypatch.setenv("JDE_AUTOMATION_PARALLEL", "0")
    rid = _new_request(client, startAnalysis=False)
    automation.queue_intake("vdb", [rid], "Jira")
    lc = client.get(f"/changes/{rid}", headers=headers("vdb")).json()["lifecycle"]
    assert lc["automation"]["state"] == "scheduled" and lc["nextAction"]["owner"] == "jade"
    assert automation.sweep("vdb", recheck=False)["started"] == []      # no free slot yet
    monkeypatch.setenv("JDE_AUTOMATION_PARALLEL", "2")
    assert automation.sweep("vdb", recheck=False)["started"] == [rid]


def test_a_switched_off_agent_is_logged_once_and_not_retried(client, automation_on, monkeypatch):
    from jde_api_service.services import agent_settings

    def off(*a, **k):
        raise agent_settings.AgentDisabled("the improve-agent is switched off for this customer")
    monkeypatch.setattr(agent_settings, "require_enabled", off)
    rid = _new_request(client)
    automation.sweep("vdb", recheck=False)
    assert automation_on == []
    skipped = [e for e in automation.events("vdb") if e["story_id"] == rid and e["action"] == "skipped"]
    assert len(skipped) == 1 and "switched off" in skipped[0]["detail"]


# ---------------------------------------------------------------------
# Reassessment end to end
# ---------------------------------------------------------------------
def test_answers_leave_the_rerun_to_the_application_manager(client, monkeypatch):
    from jde_mcp_server import approval

    from ._discovery import ready_company
    from .test_stage1_execution_safeguards import OPERATION, _approved_story, _full_scope, _save_scope

    ready_company(client, "vdb")
    _save_scope(client, "vdb", _full_scope())
    _approved_story("S-AU-1")
    _scripted_architect(monkeypatch, "S-AU-1", _summary())
    approval.propose_change("S-AU-1", {**OPERATION, "story_id": "S-AU-1"}, "processing_option_update")

    monkeypatch.setenv("JDE_ROUTINE_AUTOMATION", "true")
    monkeypatch.setenv("JDE_AUTOMATION_SETTLE_SECONDS", "0")
    started: list = []
    monkeypatch.setattr(automation, "_launch", lambda fn, kw, **_: started.append(kw))
    lc = client.get("/changes/S-AU-1", headers=headers("vdb")).json()["lifecycle"]
    issue = next(i for i in lc["issues"] if i["ownerLabel"] == "Finance")
    r = client.post(f"/changes/S-AU-1/issues/{issue['id']}/resolution", headers=headers("vdb"),
                    json={"status": "answered", "answer": "Object account 5110"})
    lc = r.json()["lifecycle"]
    assert lc["automation"] is None
    assert lc["nextAction"]["action"] == "rerun_solutioning" and lc["nextAction"]["owner"] == "product_manager"
    assert automation.sweep("vdb", recheck=False)["started"] == [] and started == []


# ---------------------------------------------------------------------
# Re-check before delivery
# ---------------------------------------------------------------------
def test_the_recheck_tells_the_manager_only_when_jde_changed(client, monkeypatch, automation_on):
    from jde_mcp_server import approval, binding

    story = SimpleNamespace(id="S-RC-1", title="Freight line", lifecycle=SimpleNamespace(phase="delivery"))
    record = {"change_id": "C-RC-1", "story_id": "S-RC-1", "company_id": "vdb", "status": "approved"}
    monkeypatch.setattr(approval, "all_records", lambda: [record])
    monkeypatch.setattr(approval, "_never_used", lambda r: True)
    found: list[str] = []
    monkeypatch.setattr(binding, "problems", lambda r, read_current=True: list(found))

    assert automation.recheck_before_delivery("vdb", [story]) == ["S-RC-1"]
    assert automation.state_of("vdb", "S-RC-1")["recheck"]["status"] == "ok"
    # Within the interval nothing is read again.
    assert automation.recheck_before_delivery("vdb", [story]) == []
    found[:] = ["the target's current state cannot be read: AIS timeout"]
    automation.recheck_before_delivery("vdb", [story], force=True)
    feed = client.get("/notifications", headers=headers("vdb")).json()
    assert feed["items"] == []                    # could not check is not "changed"
    found[:] = ["the target changed since approval: it was 'S3' when approved and is now 'SX'"]
    automation.recheck_before_delivery("vdb", [story], force=True)
    automation.recheck_before_delivery("vdb", [story], force=True)
    feed = client.get("/notifications", headers=headers("vdb")).json()
    assert len(feed["items"]) == 1 and feed["items"][0]["kind"] == "exception"
    assert "'SX'" in feed["items"][0]["text"] and feed["unread"] == 1


# ---------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------
def _lc(phase="story_review", health="waiting_decision", owner="domain_owner", action="review_story",
        kind="decision", outcome=None, summary="Review the user story"):
    return SimpleNamespace(phase=phase, health=health, outcome=outcome,
                           next_action=SimpleNamespace(owner=owner, action=action, kind=kind, summary=summary,
                                                       tab="story"))


def test_notifications_follow_the_lifecycle_for_the_right_people(client, ellen_client):
    story = SimpleNamespace(id="S-N-1", title="Freight line", business_domain_id="DOM-1")
    jade = _lc(owner="jade", action="", kind="none", health="in_progress", phase="understand")
    assert notifications.observe("vdb", story, jade) is False        # first sighting: recorded only
    assert notifications.observe("vdb", story, jade) is False        # unchanged
    assert notifications.observe("vdb", story, _lc()) is True        # JADE finished; a Domain Owner decides
    # Hendro has all roles but is not assigned to DOM-1: a Domain Owner item is not his.
    feed = client.get("/notifications", headers=headers("vdb")).json()
    assert feed["items"] == []
    assert notifications.observe("vdb", story, _lc(phase="solutioning", health="failed", owner="product_manager",
                                                   action="rerun_solutioning", kind="task",
                                                   summary="JADE could not finish")) is True
    assert notifications.observe("vdb", story, _lc(phase="done", health="done", owner="none", action="",
                                                   kind="none", outcome="delivered",
                                                   summary="Delivered and recorded.")) is True
    feed = client.get("/notifications", headers=headers("vdb")).json()
    assert [i["kind"] for i in feed["items"]] == ["completion", "exception"] and feed["unread"] == 2
    assert feed["items"][0]["storyId"] == "S-N-1" and feed["items"][0]["text"].startswith("Delivered")
    assert client.post("/notifications/read", headers=headers("vdb")).json()["unread"] == 0
    # Each person has their own read marker.
    assert ellen_client.get("/notifications", headers=headers("vdb")).json()["unread"] == 2
    # The digest is optional and per person.
    r = client.put("/notifications/preferences", headers=headers("vdb"), json={"digest": "daily"})
    assert r.json()["digest"] == "daily" and r.json()["emailAvailable"] is False
    assert notifications.send_digests("vdb", force=True) == []      # no e-mail set up: nothing is sent


def test_automation_settings_are_for_managers(client):
    r = client.get("/automation", headers=headers("vdb"))
    assert r.status_code == 200 and r.json()["enabled"] is True and r.json()["maxRunsPerDay"] == automation.MAX_RUNS
    r = client.put("/automation", headers=headers("vdb"), json={"jiraPickup": False})
    assert r.json()["jiraPickup"] is False and r.json()["enabled"] is True and r.json()["updatedBy"]
