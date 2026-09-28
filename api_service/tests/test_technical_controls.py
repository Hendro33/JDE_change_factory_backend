"""
Regression tests for the Technical workflow's controls on the RECORDED
delivery route (deterministic, scripted stand-ins for the model; synthetic
approver identities). People do the work in JD Edwards and record each
milestone in Jade; Jade re-checks everything before recording:

  the agent cannot approve or deliver its own package; only the exact,
  latest approved revision can be recorded; a failed build needs a new
  revision and a fresh approval; new design revisions, new source exports,
  drift after approval and changed object-librarian evidence block the
  recording; company/domain isolation; unsupported or partial sources are
  never prepared; concurrent and delayed agent runs; approval expiry;
  only a current CNC operator records the activation; and unresolved
  (clarification-required) outcomes.
"""

from __future__ import annotations

import time

import pytest

from ._discovery import sim_edit
from ._technical import (
    OBJECT_NAME, OLD_LINE, SOURCE, TESTS, approve_package, cnc, prepare, ready_story, record_apply, record_build,
    record_verify, upload_source,
)
from .conftest import TEST_PASSWORD, _apply_csrf_header, headers


def _change(story: str, revision: int, company: str = "vdb") -> dict:
    from jde_api_service.technical import service, store

    return service.change_for(store.get_package(company, story, revision))


def _package(story: str, revision: int, company: str = "vdb") -> dict:
    from jde_api_service.technical import store

    return store.get_package(company, story, revision)


def _save_change(record: dict) -> None:
    from jde_mcp_server import approval

    approval._save(record["change_id"], record)


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


def _refused(r, *fragments: str, status: int = 409) -> None:
    assert r.status_code == status, (r.status_code, r.text)
    detail = r.json()["detail"]
    assert all(f in detail for f in fragments), detail


# ---------------------------------------------------------------------
# The agent never approves or delivers; only the exact approved revision
# ---------------------------------------------------------------------
def test_the_agent_cannot_approve_or_deliver_its_own_package(client, monkeypatch):
    from jde_api_service.technical import service, tools as tech_tools
    from jde_api_service.technical.tools import TechnicalAgentTools

    ready_story(client, monkeypatch, "S-TC-SELF")
    prepare("vdb", "S-TC-SELF")
    run = service.start_run("vdb", "S-TC-SELF", purpose="repair", initiated_by="u-hendro")
    tools = TechnicalAgentTools(company_id="vdb", story_id="S-TC-SELF", run_id=run["run_id"])
    # No approval and no delivery capability on the agent's tools or its MCP surface.
    forbidden = ("approve", "apply", "build", "verify", "milestone", "cnc", "reconcile")
    assert not [n for n in dir(tools) if any(n.startswith(f) for f in forbidden)]
    assert not [n for n in tech_tools.TOOL_NAMES if any(f in n for f in forbidden)]
    status = tools.status(None)
    assert status["eligibility"]["eligible"] is False
    assert any("not approved" in r for r in status["eligibility"]["reasons"]), status["eligibility"]
    assert _change("S-TC-SELF", 1)["status"] == "pending"
    # Nor can anything be recorded against the unapproved package.
    _refused(record_apply(client, "S-TC-SELF", 1), "not approved")


def test_a_build_failure_needs_a_new_revision_and_a_fresh_approval(client, monkeypatch):
    from jde_mcp_server import approval, technical_gate

    ready_story(client, monkeypatch, "S-TC-BUILD")
    prepare("vdb", "S-TC-BUILD", marker=False)
    approve_package(client, "S-TC-BUILD", 1)
    assert record_apply(client, "S-TC-BUILD", 1).status_code == 200
    r = record_build(client, "S-TC-BUILD", 1, succeeded=False, log="B5542001.c(7): error C2065: 'cCreditExempt'")
    assert r.status_code == 200 and r.json()["milestone"] == "build_failed", r.text
    assert any("C2065" in line for line in r.json()["log"])
    # The failed revision never builds again and never moves on.
    _refused(record_build(client, "S-TC-BUILD", 1), "failed")
    assert cnc(client, "S-TC-BUILD", 1).status_code == 409
    assert record_verify(client, "S-TC-BUILD", 1).status_code == 409
    # The repair is a new revision; revision 1 is superseded and its approval stays as history.
    out = prepare("vdb", "S-TC-BUILD", marker=True)
    assert out["revision"] == 2
    rev1, rev2 = _package("S-TC-BUILD", 1), _package("S-TC-BUILD", 2)
    assert rev1["superseded_by"] == 2 and rev2["content"]["repair_of"]["revision"] == 1
    assert rev2["content_sha256"] != rev1["content_sha256"]
    assert approval._load(rev1["change_id"])["status"] == "approved"  # history kept
    # The superseded revision can no longer be approved or recorded.
    _refused(client.post("/changes/S-TC-BUILD/technical/packages/1/approve", headers=headers("vdb"),
                         json={"note": "x"}), "superseded")
    _refused(record_build(client, "S-TC-BUILD", 1), "superseded")
    # The changed repair cannot be recorded under the previous approval...
    with pytest.raises(approval.ChangeApprovalError, match="not the exact package approved"):
        technical_gate.apply(rev1["change_id"], rev2, actor="Synthetic", actor_user_id="u-hendro",
                             omw_project="PRJ-JADE-1", evidence_reference="x")
    # ...nor without its own approval.
    _refused(record_apply(client, "S-TC-BUILD", 2), "not approved")
    approve_package(client, "S-TC-BUILD", 2)
    assert record_apply(client, "S-TC-BUILD", 2).status_code == 200
    assert record_build(client, "S-TC-BUILD", 2).json()["milestone"] == "built"


def test_a_package_changed_after_approval_cannot_be_recorded(client, monkeypatch):
    from jde_api_service.persistence.db import connection

    ready_story(client, monkeypatch, "S-TC-TAMPER")
    prepare("vdb", "S-TC-TAMPER")
    approve_package(client, "S-TC-TAMPER", 1)
    pkg = _package("S-TC-TAMPER", 1)
    with connection(immediate=True) as conn:  # the stored checksum no longer matches what was approved
        conn.execute("UPDATE technical_packages SET content_sha256 = ? WHERE package_id = ? AND revision = 1",
                     ("0" * 64, pkg["package_id"]))
    _refused(record_apply(client, "S-TC-TAMPER", 1), "not the exact package approved")


# ---------------------------------------------------------------------
# Stale source, drift and changed baselines
# ---------------------------------------------------------------------
def test_a_superseded_source_export_cannot_be_prepared(client, monkeypatch):
    from jde_api_service.technical import service
    from jde_api_service.technical.tools import TechnicalAgentTools

    art = ready_story(client, monkeypatch, "S-TC-STALE")
    run = service.start_run("vdb", "S-TC-STALE", purpose="prepare", initiated_by="u-hendro")
    tools = TechnicalAgentTools(company_id="vdb", story_id="S-TC-STALE", run_id=run["run_id"])
    # While the run works, the customer uploads a newer export of the same object.
    newer = upload_source(client, content=SOURCE + "\n/* re-exported after a DEV change */\n")
    assert newer["artifactId"] == art["artifactId"] and newer["revision"] > art["revision"]
    ref = f"{art['artifactId']}@r{art['revision']}"
    opened = tools.open_in_workspace(ref)
    assert opened["opened"] is False and "stale" in opened["reason"], opened
    # And the design that consulted the older export is flagged: no new run starts from it.
    with pytest.raises(service.TechnicalRefused, match="reassessment"):
        service.start_run("vdb", "S-TC-STALE", purpose="prepare", initiated_by="u-hendro")


def test_a_new_source_revision_invalidates_the_approved_package(client, monkeypatch):
    ready_story(client, monkeypatch, "S-TC-ART")
    prepare("vdb", "S-TC-ART")
    approved = approve_package(client, "S-TC-ART", 1)
    upload_source(client, content=SOURCE + "\n// re-exported\n")
    record = _change("S-TC-ART", 1)
    assert record["invalidations"][0]["kind"] == "artifact_revised"
    assert record["approved_at"] == approved["approved_at"]  # history, not eligibility
    _refused(record_apply(client, "S-TC-ART", 1), "invalidated")


def test_drift_after_approval_blocks_recording_the_apply_even_without_an_invalidation(client, monkeypatch):
    """The binding re-reads the target itself: even if no invalidation had
    been recorded, the object's current source is no longer the one the
    approved package was prepared from."""
    ready_story(client, monkeypatch, "S-TC-DRIFT")
    prepare("vdb", "S-TC-DRIFT")
    approve_package(client, "S-TC-DRIFT", 1)
    upload_source(client, content=SOURCE + "\n// hotfix made directly in DEV\n")
    record = _change("S-TC-DRIFT", 1)
    record["invalidations"] = []  # take away the invalidation: the binding alone must refuse
    _save_change(record)
    r = record_apply(client, "S-TC-DRIFT", 1)
    assert r.status_code == 409, r.text
    assert "stale source" in r.json()["detail"] or "changed since approval" in r.json()["detail"], r.text
    assert _change("S-TC-DRIFT", 1)["status"] == "approved"  # history, not eligibility
    assert "applied" not in [m["milestone"] for m in _change("S-TC-DRIFT", 1).get("milestones") or []]


def test_refresh_evidence_that_changes_the_objects_librarian_row_invalidates_the_package(client, monkeypatch):
    ready_story(client, monkeypatch, "S-TC-REFRESH")
    prepare("vdb", "S-TC-REFRESH")
    approve_package(client, "S-TC-REFRESH", 1)
    with sim_edit("vdb", f"drift: {OBJECT_NAME} re-described in the object librarian") as est:
        for row in est["tables"]["F9860"]:
            if row["SIOBNM"] == OBJECT_NAME:
                row["SIMD"] = "Custom Credit Check v2"
    r = client.post("/changes/S-TC-REFRESH/architecture-review/refresh-evidence", headers=headers("vdb")).json()
    assert [w["change_id"] for w in r["manifest"]["affected_work"]] == [_change("S-TC-REFRESH", 1)["change_id"]]
    assert record_apply(client, "S-TC-REFRESH", 1).status_code == 409
    # And the flagged design no longer starts Technical Agent runs.
    run = client.post("/changes/S-TC-REFRESH/technical/runs", headers=headers("vdb"), json={"purpose": "prepare"})
    _refused(run, "reassessment")


def test_a_new_design_revision_is_not_substituted_into_approved_work(client, monkeypatch):
    from ._technical import technical_design

    ready_story(client, monkeypatch, "S-TC-DESIGN")
    prepare("vdb", "S-TC-DESIGN")
    approve_package(client, "S-TC-DESIGN", 1)
    technical_design(client, monkeypatch, "S-TC-DESIGN")  # the Architect runs again: design revision 2
    _refused(record_apply(client, "S-TC-DESIGN", 1), "design revision 1")
    # Revision 2 has no design approval yet: the Technical Agent cannot start.
    run = client.post("/changes/S-TC-DESIGN/technical/runs", headers=headers("vdb"), json={"purpose": "prepare"})
    _refused(run, "not been approved")


# ---------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------
def test_another_company_sees_nothing_and_cannot_act(client, monkeypatch):
    from jde_api_service.technical import store
    from jde_api_service.technical.tools import TechnicalAgentTools

    ready_story(client, monkeypatch, "S-TC-ISO")
    prepare("vdb", "S-TC-ISO")
    assert client.get("/changes/S-TC-ISO/technical", headers=headers("bwm")).status_code == 404
    assert client.post("/changes/S-TC-ISO/technical/packages/1/approve", headers=headers("bwm"),
                       json={"note": "x"}).status_code == 404
    approve_package(client, "S-TC-ISO", 1)
    assert record_apply(client, "S-TC-ISO", 1, company="bwm").status_code == 404
    assert record_build(client, "S-TC-ISO", 1, company="bwm").status_code == 404
    assert cnc(client, "S-TC-ISO", 1, company="bwm").status_code == 404
    assert record_verify(client, "S-TC-ISO", 1, company="bwm").status_code == 404
    assert not _change("S-TC-ISO", 1).get("milestones")
    run_id = store.runs_for("vdb", "S-TC-ISO")[0]["run_id"]
    with pytest.raises(ValueError, match="no such Technical Agent run"):
        TechnicalAgentTools(company_id="bwm", story_id="S-TC-ISO", run_id=run_id)


def test_a_source_of_another_company_or_domain_is_not_visible(client, monkeypatch):
    from jde_api_service.persistence.db import connection
    from jde_api_service.technical import service
    from jde_api_service.technical.tools import TechnicalAgentTools

    ready_story(client, monkeypatch, "S-TC-DOM")
    other_company = upload_source(client, "bwm", object_name="B5542011")
    other_domain = upload_source(client, "vdb", object_name="B5542012")
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
# Unsupported and incomplete sources
# ---------------------------------------------------------------------
@pytest.mark.parametrize("fmt,object_type,content,why", [
    ("er_text", "ER", "ER print export of P554299\nIF BC OrderTotal > BC CreditLimit\n", "changes nothing in JDE"),
    ("pdf", "BSFN", "%PDF-1.4 not really", "no safe way to edit"),
    ("c_source", "BSFN", SOURCE + "// padding\n" * 7000, "partial export"),
])
def test_unsupported_or_partial_sources_are_never_prepared(client, monkeypatch, fmt, object_type, content, why):
    from jde_api_service.technical import service
    from jde_api_service.technical.tools import TechnicalAgentTools

    story = f"S-TC-FMT-{fmt}"
    ready_story(client, monkeypatch, story)
    art = upload_source(client, content=content, fmt=fmt, object_name="B5542099", object_type=object_type)
    run = service.start_run("vdb", story, purpose="prepare", initiated_by="u-hendro")
    tools = TechnicalAgentTools(company_id="vdb", story_id=story, run_id=run["run_id"])
    info = next(a for a in tools.list_source_artifacts()["artifacts"] if a["artifact_id"] == art["artifactId"])
    assert info["can_prepare"] is False and any(why in r for r in info["reasons"]), info["reasons"]
    assert tools.open_in_workspace(info["evidence_id"])["opened"] is False


def test_only_business_function_source_can_be_applied_through_the_recorded_route():
    from jde_mcp_server import technical_gate

    assert technical_gate.format_support("c_source")["prepare"] is True
    assert technical_gate.format_support("c_source")["apply"] == {"recorded": True}
    assert technical_gate.format_support("er_text")["prepare"] is False
    assert not technical_gate.format_support("er_text")["apply"].get("recorded")
    assert technical_gate.format_support("pdf") == {"prepare": False, "apply": {}}
    assert technical_gate.format_support("jade_sim_er") == {"prepare": False, "apply": {}}


# ---------------------------------------------------------------------
# Concurrency and delayed responses
# ---------------------------------------------------------------------
def test_two_runs_cannot_work_on_one_story_at_once(client, monkeypatch):
    from jde_api_service.technical import service, store

    ready_story(client, monkeypatch, "S-TC-CONC")
    service.start_run("vdb", "S-TC-CONC", purpose="prepare", initiated_by="u-hendro")
    with pytest.raises(store.StaleSubmission, match="still running"):
        service.start_run("vdb", "S-TC-CONC", purpose="repair", initiated_by="u-hendro")


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
    source = next(a for a in listing if a["format"] == "c_source")
    opened = slow_tools.open_in_workspace(source["evidence_id"])
    slow_tools.replace(opened["file_id"], OLD_LINE, OLD_LINE + " /* MOD late */")
    out = slow_tools.submit({"explanation": "late", "test_plan": TESTS})
    assert out["submitted"] is False and "no longer active" in out["problems"][0], out
    assert [p["revision"] for p in store.packages_for("vdb", "S-TC-LATE")] == [1]


def test_a_run_that_missed_a_newer_revision_is_discarded(client, monkeypatch):
    from jde_api_service.persistence.db import connection
    from jde_api_service.technical import service, store

    ready_story(client, monkeypatch, "S-TC-CAS")
    prepare("vdb", "S-TC-CAS")
    run = service.start_run("vdb", "S-TC-CAS", purpose="repair", initiated_by="u-hendro")
    with connection(immediate=True) as conn:  # the run's view is older than what is stored
        conn.execute("UPDATE technical_runs SET expected_package_revision = 0 WHERE run_id = ?", (run["run_id"],))
    with pytest.raises(store.StaleSubmission, match="delayed result is discarded"):
        store.store_package(run_id=run["run_id"], company_id="vdb", story_id="S-TC-CAS",
                            content={"revision": 1}, content_sha256="x")
    assert [p["revision"] for p in store.packages_for("vdb", "S-TC-CAS")] == [1]


# ---------------------------------------------------------------------
# Approval expiry and the CNC checkpoint
# ---------------------------------------------------------------------
def test_an_expired_implementation_approval_blocks_every_milestone(client, monkeypatch):
    def set_expiry(at: float) -> None:
        record = _change("S-TC-EXP", 1)
        record["expires_at"] = at
        _save_change(record)

    ready_story(client, monkeypatch, "S-TC-EXP")
    prepare("vdb", "S-TC-EXP")
    approve_package(client, "S-TC-EXP", 1)
    set_expiry(time.time() - 1)
    _refused(record_apply(client, "S-TC-EXP", 1), "expired")
    # Within the approval's life each step is recorded; once it expires, the next is refused.
    set_expiry(time.time() + 3600)
    assert record_apply(client, "S-TC-EXP", 1).status_code == 200
    set_expiry(time.time() - 1)
    _refused(record_build(client, "S-TC-EXP", 1), "expired")
    set_expiry(time.time() + 3600)
    assert record_build(client, "S-TC-EXP", 1).status_code == 200
    set_expiry(time.time() - 1)
    _refused(cnc(client, "S-TC-EXP", 1), "expired")
    set_expiry(time.time() + 3600)
    assert cnc(client, "S-TC-EXP", 1).status_code == 200
    set_expiry(time.time() - 1)
    _refused(record_verify(client, "S-TC-EXP", 1), "expired")
    assert _change("S-TC-EXP", 1).get("verification") is None


def test_only_a_current_cnc_operator_can_record_the_activation(client, monkeypatch):
    from jde_mcp_server import approval, technical_gate

    from .test_concurrency_and_stale_authority import _set_roles

    ready_story(client, monkeypatch, "S-TC-CNC")
    prepare("vdb", "S-TC-CNC")
    approve_package(client, "S-TC-CNC", 1)
    record_apply(client, "S-TC-CNC", 1)
    # Before a successful build there is nothing to activate.
    _refused(cnc(client, "S-TC-CNC", 1), "not known to be built")
    record_build(client, "S-TC-CNC", 1)
    pm = _user("u-syn-pm", ["product_manager"])
    assert cnc(pm, "S-TC-CNC", 1).status_code == 403  # not a CNC operator
    operator = _user("u-syn-cnc", ["cnc_operator"])
    # Authority is re-read at the moment of recording: a revoked role cannot record it.
    _set_roles("vdb", "u-syn-cnc", ["dashboard_viewer"])
    assert cnc(operator, "S-TC-CNC", 1).status_code == 403
    # The gate itself refuses too, independent of the route's role check.
    with pytest.raises(approval.ApproverNotAuthorised):
        technical_gate.record_cnc_activation(_change("S-TC-CNC", 1)["change_id"], _package("S-TC-CNC", 1),
                                             actor_user_id="u-syn-cnc", actor_name="Synthetic", package_name="P1",
                                             evidence_reference="x")
    assert _change("S-TC-CNC", 1).get("cnc_activation") is None
    _refused(record_verify(client, "S-TC-CNC", 1), "awaiting CNC activation")
    # A current CNC operator records it.
    current = _user("u-syn-cnc2", ["cnc_operator"])
    r = cnc(current, "S-TC-CNC", 1)
    assert r.status_code == 200 and r.json()["user_id"] == "u-syn-cnc2", r.text


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
    assert tools.status(None) == {"package": None}
    # Nothing can be approved or recorded: there is no package.
    assert client.post("/changes/S-TC-CLAR/technical/packages/1/approve", headers=headers("vdb"),
                       json={"note": "x"}).status_code == 404
    assert record_apply(client, "S-TC-CLAR", 1).status_code == 404


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
    _refused(r, "not to the Technical Agent")


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


def test_the_service_starts_on_a_fresh_database(isolated_dirs, tmp_path, monkeypatch):
    """Regression: startup recovery must not read a table before the schema exists."""
    from fastapi.testclient import TestClient

    from jde_api_service.config import settings
    from jde_api_service.main import app

    fresh = tmp_path / "fresh_api_data"
    monkeypatch.setattr(settings, "data_dir", str(fresh))
    monkeypatch.setenv("JDE_AUTH_DB_PATH", str(fresh / "jde.sqlite3"))
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
