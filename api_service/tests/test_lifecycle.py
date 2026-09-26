"""The canonical story lifecycle (services/lifecycle.py): one phase, one
health and one next action per story, derived only from the authoritative
records. These tests pin the mapping so every screen that reads it agrees."""

from __future__ import annotations

import pytest

from jde_api_service.models.change import (
    ApprovalRecord, ArchitectDecision, Change, ExactChange, ExecutionStatus,
)
from jde_api_service.services import lifecycle


def _change(**kw) -> Change:
    base = dict(id="S-1", customer_id="c1", title="t", source="Business", state="APPROVED",
                created_at="2026-09-01T00:00:00+00:00", updated_at="2026-09-01T00:00:00+00:00")
    base.update(kw)
    return Change(**base)


def _decision(route: str) -> ArchitectDecision:
    return ArchitectDecision(recommended_route=route, confidence=0.8, decided_at="2026-09-01T00:00:00+00:00")


@pytest.fixture(autouse=True)
def _no_records(monkeypatch):
    """Each test states the facts it needs; nothing is read from disk."""
    monkeypatch.setattr(lifecycle, "_design_facts", lambda c, s: {"baseline": {"status": "current"}, "design_approved": False})
    monkeypatch.setattr(lifecycle, "_technical_facts", lambda c, s: {"package": None, "record": None, "running": False})
    monkeypatch.setattr(lifecycle, "_asbuilt_final", lambda c, s: None)
    monkeypatch.setattr(lifecycle, "_process_decided", lambda c, s: None)


def test_new_request_waits_for_analysis():
    lc = lifecycle.derive(_change(id="CR-1", state="RECEIVED"))
    assert (lc.phase, lc.health, lc.next_action.action) == ("understand", "waiting", "start_analysis")
    assert lc.next_action.owner == "product_manager"


def test_running_analysis_is_jade_working_not_a_task():
    lc = lifecycle.derive(_change(id="CR-1", state="REFINING", processing_stage="improving"))
    assert (lc.phase, lc.health, lc.next_action.owner, lc.next_action.kind) == ("understand", "in_progress", "jade", "none")


def test_failed_analysis_needs_attention():
    lc = lifecycle.derive(_change(id="CR-1", state="RECEIVED", processing_stage="failed", processing_error="boom"))
    assert (lc.phase, lc.health) == ("understand", "failed")


@pytest.mark.parametrize("stage,owner,action", [
    (None, "domain_owner", "review_story"),
    ("domain_owner_reviewing", "domain_owner", "review_story"),
    ("ready_for_application_manager", "product_manager", "authorise_delivery"),
])
def test_story_review_owners(stage, owner, action):
    lc = lifecycle.derive(_change(state="BACKLOG_READY", business_domain_id="D1", domain_review_stage=stage))
    assert (lc.phase, lc.health, lc.next_action.owner, lc.next_action.action) == (
        "story_review", "waiting_decision", owner, action)


def test_story_without_domain_asks_for_one():
    lc = lifecycle.derive(_change(state="BACKLOG_READY"))
    assert lc.next_action.action == "assign_domain"


def test_rejections_are_done_not_in_progress():
    assert lifecycle.derive(_change(state="REJECTED")).outcome == "rejected"
    lc = lifecycle.derive(_change(state="BACKLOG_READY", business_domain_id="D1", domain_review_stage="domain_owner_rejected"))
    assert (lc.phase, lc.outcome) == ("done", "rejected")


def test_approved_without_analysis_is_a_task_not_fake_progress():
    lc = lifecycle.derive(_change())
    assert (lc.phase, lc.health, lc.next_action.owner) == ("solutioning", "waiting", "product_manager")
    lc = lifecycle.derive(_change(architecture_review_stage="analyzing"))
    assert (lc.phase, lc.health, lc.next_action.owner) == ("solutioning", "in_progress", "jade")


def test_failed_solutioning_gives_business_wording():
    lc = lifecycle.derive(_change(architecture_review_stage="failed",
                                  architecture_review_error="this customer has no AI connection. An Admin sets it up"))
    assert lc.health == "failed"
    assert "AI connection" in lc.open_items[0] and "Admin sets" not in lc.open_items[0]


def _functional(write="ready", test="ready", approved=True, executable=True):
    return _change(
        architecture_review_stage="done", architect_decision=_decision("Functional Agent"),
        exact_change=ExactChange(tool="set_processing_option", application="P4210", version="V1", option="1",
                                 capability_executable=executable,
                                 execution=ExecutionStatus(write_state=write, test_state=test)),
        change_approval=ApprovalRecord(approval_id="C1", kind="change", status="approved") if approved else None,
    )


def test_functional_route_progression():
    assert lifecycle.derive(_functional(approved=False)).next_action.action == "approve_exact_change"
    assert lifecycle.derive(_functional(approved=False)).phase == "solution_review"
    assert (lifecycle.derive(_functional()).phase, lifecycle.derive(_functional()).health) == ("delivery", "in_progress")
    assert lifecycle.derive(_functional(write="applied")).phase == "validation"
    released = lifecycle.derive(_functional(write="applied", test="completed"))
    assert (released.phase, released.next_action.action) == ("release", "finalise_asbuilt")
    assert lifecycle.derive(_functional(write="unknown")).health == "failed"
    assert lifecycle.derive(_functional(executable=False)).health == "blocked"


def test_functional_done_when_asbuilt_final(monkeypatch):
    monkeypatch.setattr(lifecycle, "_asbuilt_final", lambda c, s: {"status": "final"})
    lc = lifecycle.derive(_functional(write="applied", test="completed"))
    assert (lc.phase, lc.health, lc.outcome) == ("done", "done", "delivered")
    assert [s.state for s in lc.delivery_steps] == ["done", "done", "done", "todo"]


def _technical(monkeypatch, *, design_approved=True, package=True, status="approved", apply="applied", build="built",
               cnc=True, verification=None, running=False):
    monkeypatch.setattr(lifecycle, "_design_facts",
                        lambda c, s: {"baseline": {"status": "current"}, "design_approved": design_approved})
    record = {"status": status, "cnc_activation": {"by": "x"} if cnc else None, "verification": verification}
    monkeypatch.setattr(lifecycle, "_technical_facts", lambda c, s: {
        "package": {"revision": 1} if package else None, "record": record if package else None,
        "states": {"apply": apply, "build": build}, "running": running, "verify_state": "ready"})
    return lifecycle.derive(_change(architecture_review_stage="done", architect_decision=_decision("Technical Agent")))


def test_technical_route_progression(monkeypatch):
    assert _technical(monkeypatch, design_approved=False).next_action.action == "approve_design"
    assert _technical(monkeypatch, package=False).next_action.action == "start_technical_prepare"
    assert _technical(monkeypatch, status="proposed").next_action.action == "approve_package"
    assert _technical(monkeypatch, apply="ready", build="ready").next_action.action == "start_technical_execute"
    cnc = _technical(monkeypatch, cnc=False)
    assert (cnc.phase, cnc.next_action.owner) == ("delivery", "cnc_operator")
    assert _technical(monkeypatch).phase == "validation"
    failed = _technical(monkeypatch, verification={"passed": False, "runtime_is_approved_artifact": True, "results": []})
    assert (failed.phase, failed.health) == ("validation", "failed")
    ok = _technical(monkeypatch, verification={"passed": True, "runtime_is_approved_artifact": True,
                                               "results": [{"passed": True}] * 4})
    assert (ok.phase, ok.next_action.action) == ("release", "finalise_asbuilt")
    assert ok.delivery_steps[1].detail == "4 / 4 tests passed"


def test_my_work_ownership():
    lc = lifecycle.derive(_change(state="BACKLOG_READY", business_domain_id="D1"))
    assert lifecycle.is_mine(lc, {"domain_owner"})
    assert not lifecycle.is_mine(lc, {"product_manager"})
    working = lifecycle.derive(_change(architecture_review_stage="analyzing"))
    assert not lifecycle.is_mine(working, {"admin", "product_manager", "domain_owner"})


def test_delivered_story_stays_done_when_design_is_flagged_later(monkeypatch):
    monkeypatch.setattr(lifecycle, "_asbuilt_final", lambda c, s: {"status": "final"})
    monkeypatch.setattr(lifecycle, "_design_facts", lambda c, s: {"baseline": {"status": "needs_reassessment"}, "design_approved": True})
    lc = lifecycle.derive(_functional(write="applied", test="completed"))
    assert (lc.phase, lc.outcome) == ("done", "delivered")
    assert lc.open_items and "reassessment" in lc.open_items[0]


def test_proposed_exact_change_without_analysis_stage_is_a_solution_decision():
    change = _functional(approved=False)
    change.architecture_review_stage = None
    change.architect_decision = None
    lc = lifecycle.derive(change)
    assert (lc.phase, lc.next_action.action) == ("solution_review", "approve_exact_change")
