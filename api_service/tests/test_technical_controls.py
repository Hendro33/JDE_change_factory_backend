"""
Regression tests for the Technical workflow's controls (deterministic,
scripted stand-ins for the model; synthetic approver identities):

  build failure and repair, stale source and changed baselines,
  company/domain isolation, unsupported or incomplete artifacts,
  concurrent and delayed updates, interrupted application and blind
  retry, approval expiry, unauthorised CNC completion, and unresolved
  (clarification-required) outcomes.
"""

from __future__ import annotations

import time

import pytest

from ._discovery import sim_edit
from ._technical import (
    ENV, OBJECT_KEY, OLD_LINE, SOURCE, approve_package, cnc, milestone, prepare, ready_story, upload_source,
)
from .conftest import TEST_PASSWORD, _apply_csrf_header, headers


def _change(story: str, revision: int, company: str = "vdb") -> dict:
    from jde_api_service.technical import service, store

    return service.change_for(store.get_package(company, story, revision))


def _package(story: str, revision: int, company: str = "vdb") -> dict:
    from jde_api_service.technical import store

    return store.get_package(company, story, revision)


def _user(user_id: str, roles: list[str], company: str = "vdb"):
    from fastapi.testclient import TestClient

    from jde_api_service.main import app
    from jde_api_service.services import auth_service, membership_service

    auth_service.create_user(f"{user_id}@synthetic.test", TEST_PASSWORD, f"Synthetic {user_id}", user_id=user_id)
    membership_service.create_membership(user_id, company, roles, created_by="u-hendro")
    c = TestClient(app)
    assert c.post("/auth/login", json={"email": f"{user_id}@synthetic.test", "password": TEST_PASSWORD}).status_code == 200
    _apply_csrf_header(c)
    return c


# ---------------------------------------------------------------------
# Build failure, repair, and approval of the exact revision only
# ---------------------------------------------------------------------
def test_a_build_failure_is_surfaced_and_a_changed_repair_needs_fresh_approval(client, monkeypatch):
    from jde_mcp_server import approval, technical_gate

    ready_story(client, monkeypatch, "S-TC-BUILD")
    prepare("vdb", "S-TC-BUILD", marker=False)  # misses the customer's modification-marker rule
    approve_package(client, "S-TC-BUILD", 1)
    assert milestone(client, "S-TC-BUILD", 1, "apply").status_code == 200
    r = milestone(client, "S-TC-BUILD", 1, "build").json()
    assert r["milestone"] == "build_failed" and any("SIM-BLD-1" in line for line in r["log"])
    # The failed revision never builds or runs again.
    again = milestone(client, "S-TC-BUILD", 1, "build")
    assert again.status_code == 409 and "failed" in again.json()["detail"]
    # The repair is a new revision; revision 1 is superseded and its approval stays as history.
    out = prepare("vdb", "S-TC-BUILD", marker=True)
    assert out["revision"] == 2
    rev1, rev2 = _package("S-TC-BUILD", 1), _package("S-TC-BUILD", 2)
    assert rev1["superseded_by"] == 2 and rev2["content"]["repair_of"]["revision"] == 1
    assert rev2["content_sha256"] != rev1["content_sha256"]
    assert approval._load(rev1["change_id"])["status"] == "approved"  # history kept
    # The changed repair cannot run under the previous approval...
    with pytest.raises(approval.ChangeApprovalError, match="not the exact package approved"):
        technical_gate.apply(rev1["change_id"], rev2, actor="test")
    # ...nor without its own approval.
    r = milestone(client, "S-TC-BUILD", 2, "apply")
    assert r.status_code == 409 and "not approved" in r.json()["detail"]
    approve_package(client, "S-TC-BUILD", 2)
    assert milestone(client, "S-TC-BUILD", 2, "apply").status_code == 200
    assert milestone(client, "S-TC-BUILD", 2, "build").json()["milestone"] == "built"


def test_the_agent_cannot_approve_its_own_package(client, monkeypatch):
    from jde_api_service.technical import service
    from jde_api_service.technical.tools import TechnicalAgentTools

    ready_story(client, monkeypatch, "S-TC-SELF")
    prepare("vdb", "S-TC-SELF")
    run = service.start_run("vdb", "S-TC-SELF", purpose="execute", initiated_by="u-hendro")
    tools = TechnicalAgentTools(company_id="vdb", story_id="S-TC-SELF", run_id=run["run_id"])
    assert not any(n for n in dir(tools) if n.startswith("approve"))
    result = tools.milestone("apply", None)
    assert result["blocked"] and "not approved" in result["reason"]
    assert _change("S-TC-SELF", 1)["status"] == "pending"


# ---------------------------------------------------------------------
# Stale source, drift and changed baselines
# ---------------------------------------------------------------------
def test_a_source_that_is_not_the_active_runtime_cannot_be_prepared(client, monkeypatch):
    from jde_api_service.technical import service
    from jde_api_service.technical.tools import TechnicalAgentTools

    ready_story(client, monkeypatch, "S-TC-STALE")
    with sim_edit("vdb", "drift: the active DEV object changed after the export") as est:
        est["objects"][OBJECT_KEY]["active"]["sha256"] = "0" * 64
    run = service.start_run("vdb", "S-TC-STALE", purpose="prepare", initiated_by="u-hendro")
    tools = TechnicalAgentTools(company_id="vdb", story_id="S-TC-STALE", run_id=run["run_id"])
    listing = tools.list_source_artifacts()["artifacts"]
    assert listing[0]["runtime_check"]["state"] == "stale" and not listing[0]["can_prepare"]
    opened = tools.open_in_workspace(listing[0]["evidence_id"])
    assert opened["opened"] is False and "stale" in opened["reason"]


def test_drift_after_approval_blocks_application(client, monkeypatch):
    ready_story(client, monkeypatch, "S-TC-DRIFT")
    prepare("vdb", "S-TC-DRIFT")
    approve_package(client, "S-TC-DRIFT", 1)
    with sim_edit("vdb", "drift: someone changes the active object in DEV after approval") as est:
        est["objects"][OBJECT_KEY]["active"]["source"] += "\n// hotfix\n"
        est["objects"][OBJECT_KEY]["active"]["sha256"] = "f" * 64
    r = milestone(client, "S-TC-DRIFT", 1, "apply")
    assert r.status_code == 409 and "changed since approval" in r.json()["detail"]
    assert _change("S-TC-DRIFT", 1)["status"] == "approved"  # history, not eligibility


def test_a_new_source_revision_invalidates_the_approved_package(client, monkeypatch):
    ready_story(client, monkeypatch, "S-TC-ART")
    prepare("vdb", "S-TC-ART")
    approved = approve_package(client, "S-TC-ART", 1)
    upload_source(client, content=SOURCE + "\n// re-exported\n")
    record = _change("S-TC-ART", 1)
    assert record["invalidations"][0]["kind"] == "artifact_revised"
    assert record["approved_at"] == approved["approved_at"]
    r = milestone(client, "S-TC-ART", 1, "apply")
    assert r.status_code == 409 and "invalidated" in r.json()["detail"]


def test_refresh_evidence_that_changes_the_objects_librarian_row_invalidates_the_package(client, monkeypatch):
    ready_story(client, monkeypatch, "S-TC-REFRESH")
    prepare("vdb", "S-TC-REFRESH")
    approve_package(client, "S-TC-REFRESH", 1)
    with sim_edit("vdb", "drift: P554210 re-described in the object librarian") as est:
        for row in est["tables"]["F9860"]:
            if row["SIOBNM"] == "P554210":
                row["SIMD"] = "Custom Sales Order Review v2"
    r = client.post("/changes/S-TC-REFRESH/architecture-review/refresh-evidence", headers=headers("vdb")).json()
    assert [w["change_id"] for w in r["manifest"]["affected_work"]] == [_change("S-TC-REFRESH", 1)["change_id"]]
    assert milestone(client, "S-TC-REFRESH", 1, "apply").status_code == 409
    # And the flagged design no longer starts Technical Agent runs.
    run = client.post("/changes/S-TC-REFRESH/technical/runs", headers=headers("vdb"), json={"purpose": "prepare"})
    assert run.status_code == 409 and "reassessment" in run.json()["detail"]


def test_a_new_design_revision_is_not_substituted_into_approved_work(client, monkeypatch):
    from ._technical import technical_design

    ready_story(client, monkeypatch, "S-TC-DESIGN")
    prepare("vdb", "S-TC-DESIGN")
    approve_package(client, "S-TC-DESIGN", 1)
    technical_design(client, monkeypatch, "S-TC-DESIGN")  # the Architect runs again: design revision 2
    r = milestone(client, "S-TC-DESIGN", 1, "apply")
    assert r.status_code == 409 and "design revision 1" in r.json()["detail"]
    # Revision 2 has no design approval yet: the Technical Agent cannot start.
    run = client.post("/changes/S-TC-DESIGN/technical/runs", headers=headers("vdb"), json={"purpose": "prepare"})
    assert run.status_code == 409 and "not been approved" in run.json()["detail"]


# ---------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------
def test_another_company_sees_nothing_and_cannot_act(client, monkeypatch):
    from jde_api_service.technical.tools import TechnicalAgentTools

    ready_story(client, monkeypatch, "S-TC-ISO")
    prepare("vdb", "S-TC-ISO")
    assert client.get("/changes/S-TC-ISO/technical", headers=headers("bwm")).status_code == 404
    assert client.post("/changes/S-TC-ISO/technical/packages/1/approve", headers=headers("bwm"),
                       json={"note": "x"}).status_code == 404
    from jde_api_service.technical import store

    run_id = store.runs_for("vdb", "S-TC-ISO")[0]["run_id"]
    with pytest.raises(ValueError, match="no such Technical Agent run"):
        TechnicalAgentTools(company_id="bwm", story_id="S-TC-ISO", run_id=run_id)


def test_a_source_of_another_company_or_domain_is_not_visible(client, monkeypatch):
    from jde_api_service.technical import service
    from jde_api_service.technical.tools import TechnicalAgentTools

    ready_story(client, monkeypatch, "S-TC-DOM")
    other_company = upload_source(client, "bwm", object_name="P554211")
    other_domain = upload_source(client, "vdb", object_name="P554212")
    from jde_api_service.persistence.db import connection

    with connection(immediate=True) as conn:  # scope that export to a domain this story is not in
        conn.execute("UPDATE technical_artifacts SET domain_id = 'DOM-ELSEWHERE' WHERE artifact_id = ?",
                     (other_domain["artifactId"],))
    run = service.start_run("vdb", "S-TC-DOM", purpose="prepare", initiated_by="u-hendro")
    tools = TechnicalAgentTools(company_id="vdb", story_id="S-TC-DOM", run_id=run["run_id"])
    visible = {a["artifact_id"] for a in tools.list_source_artifacts()["artifacts"]}
    assert other_company["artifactId"] not in visible and other_domain["artifactId"] not in visible
    for art in (other_company, other_domain):
        ref = f"{art['artifactId']}@r{art['revision']}"
        assert tools.read_source_artifact(ref) == {"available": False,
                                                   "reason": "no such artifact for this story's company and domain"}
        assert tools.open_in_workspace(ref)["opened"] is False


# ---------------------------------------------------------------------
# Unsupported and incomplete artifacts
# ---------------------------------------------------------------------
@pytest.mark.parametrize("fmt,content,why", [
    ("er_text", "ER print export of P554210\nIF BC OrderTotal > BC CreditLimit\n", "editing it as text changes nothing"),
    ("pdf", "%PDF-1.4 not really", "no safe way to edit"),
    ("jade_sim_er", SOURCE + "// padding\n" * 7000, "partial export"),
])
def test_unsupported_or_partial_sources_are_never_patched(client, monkeypatch, fmt, content, why):
    from jde_api_service.technical import service
    from jde_api_service.technical.tools import TechnicalAgentTools

    ready_story(client, monkeypatch, f"S-TC-FMT-{fmt}")
    art = upload_source(client, content=content, fmt=fmt, object_name="P554299")
    run = service.start_run("vdb", f"S-TC-FMT-{fmt}", purpose="prepare", initiated_by="u-hendro")
    tools = TechnicalAgentTools(company_id="vdb", story_id=f"S-TC-FMT-{fmt}", run_id=run["run_id"])
    info = next(a for a in tools.list_source_artifacts()["artifacts"] if a["artifact_id"] == art["artifactId"])
    assert info["can_prepare"] is False and any(why in r for r in info["reasons"]), info["reasons"]
    assert tools.open_in_workspace(info["evidence_id"])["opened"] is False


def test_business_function_source_can_be_prepared_but_never_applied(client, monkeypatch):
    from jde_mcp_server import technical_gate

    support = technical_gate.format_support("c_source")
    assert support["prepare"] is True and support["apply"] == {"simulation": False, "live": False}


def test_live_application_is_explicitly_unavailable(client, monkeypatch):
    import dataclasses

    from jde_mcp_server import technical_gate

    ready_story(client, monkeypatch, "S-TC-LIVE")
    prepare("vdb", "S-TC-LIVE")
    approve_package(client, "S-TC-LIVE", 1)
    monkeypatch.setattr(technical_gate, "settings", dataclasses.replace(technical_gate.settings, mock_mode=False))
    with pytest.raises(technical_gate.ChangeApprovalError, match="simulation application only|no qualified live"):
        technical_gate.apply(_change("S-TC-LIVE", 1)["change_id"], _package("S-TC-LIVE", 1), actor="test")


# ---------------------------------------------------------------------
# Concurrency and delayed responses
# ---------------------------------------------------------------------
def test_two_runs_cannot_work_on_one_story_at_once(client, monkeypatch):
    from jde_api_service.technical import service, store

    ready_story(client, monkeypatch, "S-TC-CONC")
    service.start_run("vdb", "S-TC-CONC", purpose="prepare", initiated_by="u-hendro")
    with pytest.raises(store.StaleSubmission, match="still running"):
        service.start_run("vdb", "S-TC-CONC", purpose="prepare", initiated_by="u-hendro")


def test_a_delayed_response_never_overwrites_a_newer_revision(client, monkeypatch):
    from jde_api_service.technical import service, store
    from jde_api_service.technical.tools import TechnicalAgentTools

    ready_story(client, monkeypatch, "S-TC-LATE")
    slow = service.start_run("vdb", "S-TC-LATE", purpose="prepare", initiated_by="u-hendro")
    slow_tools = TechnicalAgentTools(company_id="vdb", story_id="S-TC-LATE", run_id=slow["run_id"])
    # The slow run is abandoned (e.g. restart recovery) and a newer run stores revision 1.
    store.finish_run(slow["run_id"], status="failed", error="interrupted")
    prepare("vdb", "S-TC-LATE")
    # The slow run's response arrives late: nothing is stored.
    listing = slow_tools.list_source_artifacts()["artifacts"]
    opened = slow_tools.open_in_workspace(listing[0]["evidence_id"])
    slow_tools.replace(opened["file_id"], OLD_LINE, OLD_LINE + " // MOD late")
    out = slow_tools.submit({"explanation": "late", "test_plan": [
        {"name": n, "kind": k, "event": "OK_Button_Clicked", "inputs": {}, "expected": {}}
        for n, k in (("a", "positive"), ("b", "negative"), ("c", "neighbouring"))]})
    assert out["submitted"] is False and "no longer active" in out["problems"][0]
    assert [p["revision"] for p in store.packages_for("vdb", "S-TC-LATE")] == [1]


def test_a_run_that_missed_a_newer_revision_is_discarded(client, monkeypatch):
    from jde_api_service.persistence.db import connection
    from jde_api_service.technical import service, store

    ready_story(client, monkeypatch, "S-TC-CAS")
    prepare("vdb", "S-TC-CAS")
    run = service.start_run("vdb", "S-TC-CAS", purpose="prepare", initiated_by="u-hendro")
    with connection(immediate=True) as conn:  # the run's view is older than what is stored
        conn.execute("UPDATE technical_runs SET expected_package_revision = 0 WHERE run_id = ?", (run["run_id"],))
    with pytest.raises(store.StaleSubmission, match="delayed result is discarded"):
        store.store_package(run_id=run["run_id"], company_id="vdb", story_id="S-TC-CAS",
                            content={"revision": 1}, content_sha256="x")


# ---------------------------------------------------------------------
# Interrupted application and blind retry
# ---------------------------------------------------------------------
def test_an_interrupted_apply_blocks_retry_until_reconciled_against_the_estate(client, monkeypatch):
    from jde_mcp_server import sim_estate

    ready_story(client, monkeypatch, "S-TC-UNK")
    prepare("vdb", "S-TC-UNK")
    approve_package(client, "S-TC-UNK", 1)
    sim_estate.add_fault("vdb", ENV, operation="apply", target=OBJECT_KEY, mode="timeout_after_apply")
    r = milestone(client, "S-TC-UNK", 1, "apply")
    assert r.status_code == 409 and "TEST CONDITION" in r.json()["detail"]
    retry = milestone(client, "S-TC-UNK", 1, "apply")
    assert retry.status_code == 409 and "unknown" in retry.json()["detail"]
    rec = client.post("/changes/S-TC-UNK/technical/packages/1/reconcile", headers=headers("vdb"),
                      json={"milestone": "apply", "note": "read the estate"}).json()
    assert rec["outcome"] == "applied" and rec["evidence_entry_hash"]
    assert milestone(client, "S-TC-UNK", 1, "apply").status_code == 409  # applied: never twice
    assert milestone(client, "S-TC-UNK", 1, "build").json()["milestone"] == "built"


def test_an_apply_lost_before_reaching_dev_may_run_again_after_every_check(client, monkeypatch):
    from jde_mcp_server import sim_estate

    ready_story(client, monkeypatch, "S-TC-LOST")
    prepare("vdb", "S-TC-LOST")
    approve_package(client, "S-TC-LOST", 1)
    sim_estate.add_fault("vdb", ENV, operation="apply", target=OBJECT_KEY, mode="timeout_before_apply")
    assert milestone(client, "S-TC-LOST", 1, "apply").status_code == 409
    rec = client.post("/changes/S-TC-LOST/technical/packages/1/reconcile", headers=headers("vdb"),
                      json={"milestone": "apply"}).json()
    assert rec["outcome"] == "not_applied"
    assert milestone(client, "S-TC-LOST", 1, "apply").status_code == 200


# ---------------------------------------------------------------------
# Approval expiry and the CNC checkpoint
# ---------------------------------------------------------------------
def test_an_expired_implementation_approval_blocks_every_milestone(client, monkeypatch):
    from jde_mcp_server import approval

    ready_story(client, monkeypatch, "S-TC-EXP")
    prepare("vdb", "S-TC-EXP")
    approve_package(client, "S-TC-EXP", 1)
    record = _change("S-TC-EXP", 1)
    record["expires_at"] = time.time() - 1
    approval._save(record["change_id"], record)
    r = milestone(client, "S-TC-EXP", 1, "apply")
    assert r.status_code == 409 and "expired" in r.json()["detail"]


def test_only_a_current_cnc_operator_can_record_the_activation(client, monkeypatch):
    ready_story(client, monkeypatch, "S-TC-CNC")
    prepare("vdb", "S-TC-CNC")
    approve_package(client, "S-TC-CNC", 1)
    milestone(client, "S-TC-CNC", 1, "apply")
    # Before a successful build there is nothing to activate.
    assert cnc(client, "S-TC-CNC", 1).status_code == 409
    milestone(client, "S-TC-CNC", 1, "build")
    pm = _user("u-syn-pm", ["product_manager"])
    assert cnc(pm, "S-TC-CNC", 1).status_code == 403  # not a CNC operator
    operator = _user("u-syn-cnc", ["cnc_operator"])
    from .test_concurrency_and_stale_authority import _set_roles

    # Authority is re-read at the moment of recording: a revoked role cannot record it.
    _set_roles("vdb", "u-syn-cnc", ["dashboard_viewer"])
    assert cnc(operator, "S-TC-CNC", 1).status_code == 403
    # The gate itself refuses too, independent of the route's role check.
    from jde_mcp_server import approval, technical_gate

    with pytest.raises(approval.ApproverNotAuthorised):
        technical_gate.record_cnc_activation(_change("S-TC-CNC", 1)["change_id"], _package("S-TC-CNC", 1),
                                             actor_user_id="u-syn-cnc", actor_name="Synthetic", package_name="P1",
                                             evidence_reference="x")
    assert milestone(client, "S-TC-CNC", 1, "verify").status_code == 409  # still awaiting the CNC


def test_the_bootstrap_admin_is_not_a_cnc_operator_by_default():
    from jde_api_service.models.auth import BOOTSTRAP_ROLES

    assert "cnc_operator" not in BOOTSTRAP_ROLES


# ---------------------------------------------------------------------
# Unresolved outcomes are results, not failures -- and nothing proceeds
# ---------------------------------------------------------------------
def test_a_clarification_required_outcome_is_recorded_and_nothing_proceeds(client, monkeypatch):
    from jde_api_service.technical import service, store
    from jde_api_service.technical.tools import TechnicalAgentTools

    ready_story(client, monkeypatch, "S-TC-CLAR")
    run = service.start_run("vdb", "S-TC-CLAR", purpose="prepare", initiated_by="u-hendro")
    tools = TechnicalAgentTools(company_id="vdb", story_id="S-TC-CLAR", run_id=run["run_id"])
    out = tools.report_outcome("clarification_required",
                               "The story says exempt customers are held today, but the source shows no exemption.",
                               ["Should exempt customers ever be held?"])
    assert out["recorded"] is True
    store.finish_run(run["run_id"], status="completed", outcome=tools.outcome)
    saved = store.get_run(run["run_id"])
    assert saved["status"] == "completed" and saved["outcome"]["kind"] == "clarification_required"
    assert saved["outcome"]["questions"] == ["Should exempt customers ever be held?"]
    assert store.packages_for("vdb", "S-TC-CLAR") == []
    assert tools.milestone("apply", None)["blocked"] is True


def test_a_contradictory_story_design_is_a_safe_unresolved_outcome(client, monkeypatch):
    """Regression: the contradictory credit-hold story. The Architect's
    Clarification Required result is recorded as a result; nothing can be
    approved or executed from it."""
    from ._technical import technical_design

    ready_story(client, monkeypatch, "S-TC-CONTRA", approve=False)
    technical_design(client, monkeypatch, "S-TC-CONTRA", route="Clarification Required")
    view = client.get("/changes/S-TC-CONTRA/technical", headers=headers("vdb")).json()
    assert view["assignment"]["route"] == "Clarification Required"
    r = client.post("/changes/S-TC-CONTRA/technical/approve-design", headers=headers("vdb"),
                    json={"designRevision": view["assignment"]["design_revision"]})
    assert r.status_code == 409 and "not to the Technical Agent" in r.json()["detail"]


def test_an_architect_answer_in_prose_is_kept_and_labelled_not_treated_as_a_design(client, monkeypatch):
    import asyncio

    import claude_agent_sdk as sdk

    from jde_api_service.config import settings
    from jde_api_service.services import architecture_driver
    from jde_api_service.services.registry import get_architecture_review_service

    from .test_stage1_execution_safeguards import _approved_story

    _approved_story("S-TC-PROSE")

    async def prose(prompt, options):
        yield sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=2,
                                session_id="t", result="I stopped: the evidence contradicts the story's premise.")

    monkeypatch.setattr(sdk, "query", prose)
    service = get_architecture_review_service()
    asyncio.run(architecture_driver.run_architecture_review(story_id="S-TC-PROSE", repo_root=settings.repo_root,
                                                            run_service=service, customer_id="vdb"))
    run = service.get("S-TC-PROSE")
    assert run.stage == "failed" and run.error.startswith("UNSTRUCTURED RESULT (not a runtime error)")
    assert "contradicts the story's premise" in run.error
