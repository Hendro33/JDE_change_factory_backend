"""
The Technical workflow end to end, deterministic: a scripted Architect and a
scripted Technical Agent (stand-ins for the model at the model boundary) use
the same run-bound tools as the real ones; everything else is the real code
-- design approval, package storage, exact implementation approval and the
RECORDED delivery route: the Application Manager records the check-in, the
build and the verification, the CNC records the activation, and Jade
re-checks everything before recording each step. All approvals are by
synthetic test identities.
"""

from __future__ import annotations

from ._technical import (
    ALL_PASSED,
    OBJECT_KEY,
    OBJECT_NAME,
    TESTS,
    approve_package,
    cnc,
    prepare,
    ready_story,
    record_apply,
    record_build,
    record_verify,
)
from .conftest import headers


def _view(client, story: str, company: str = "vdb") -> dict:
    r = client.get(f"/changes/{story}/technical", headers=headers(company))
    assert r.status_code == 200, r.text
    return r.json()


def test_a_prepared_package_is_a_developer_ready_specification_and_only_prepared(client, monkeypatch):
    art = ready_story(client, monkeypatch, "S-TECH-1")
    out = prepare("vdb", "S-TECH-1")
    assert out["submitted"] and out["revision"] == 1
    view = _view(client, "S-TECH-1")
    assert view["mode"] == "recorded"
    pkg = view["packages"][0]
    content = pkg["content"]
    assert content["design"]["design_revision"] == view["assignment"]["design_revision"]
    assert content["design"]["baseline_id"] == view["assignment"]["design_approval"]["baseline_id"]
    assert content["objects"] == [{"object_key": OBJECT_KEY, "object_name": OBJECT_NAME, "object_type": "BSFN",
                                   "system_code": "55", "format": "c_source"}]
    source = content["sources"][0]
    assert (source["artifact_id"], source["sha256"]) == (art["artifactId"], art["sha256"])
    assert source["classification"] == "runtime_export_attested"  # the customer's statement, not a check by Jade
    cand = content["candidates"][0]
    assert cand["before_sha256"] == art["sha256"] and cand["after_sha256"] != cand["before_sha256"]
    assert "lpDS->cCreditExempt != 'Y')" in cand["diff"] and "/* MOD S-TECH-1 */" in cand["diff"]
    assert {t["kind"] for t in content["test_plan"]} == {"positive", "negative", "neighbouring"}
    assert content["explanation"] and content["requirement_trace"] and content["recovery"]["plan"]
    # Prepared only: awaiting a person's exact implementation approval; nothing recorded.
    assert pkg["approval"]["status"] == "pending"
    assert pkg["eligibility"]["eligible"] is False
    assert pkg["approval"]["milestone_states"] == {"apply": "ready", "build": "ready", "verify": "ready",
                                                   "cnc": "not recorded"}
    # The original artifact is untouched.
    from jde_api_service.discovery import artifacts

    assert artifacts.get("vdb", art["artifactId"])["sha256"] == art["sha256"]


def test_the_full_recorded_delivery_with_separate_milestones(client, monkeypatch):
    from jde_mcp_server.evidence import entries, verify_chain

    ready_story(client, monkeypatch, "S-TECH-2")
    prepare("vdb", "S-TECH-2")
    approve_package(client, "S-TECH-2", 1)
    r = record_build(client, "S-TECH-2", 1)
    assert r.status_code == 409 and "nothing is known to be checked in" in r.json()["detail"]
    r = record_apply(client, "S-TECH-2", 1)
    assert r.status_code == 200 and r.json()["active"] is False, r.text
    assert record_apply(client, "S-TECH-2", 1).status_code == 409  # never recorded twice
    r = record_build(client, "S-TECH-2", 1)
    assert r.json()["milestone"] == "built", r.text
    r = record_verify(client, "S-TECH-2", 1)
    assert r.status_code == 409 and "awaiting CNC activation" in r.json()["detail"]
    r = cnc(client, "S-TECH-2", 1)
    assert r.status_code == 200 and r.json()["package_name"] == "DV920TECH01", r.text
    assert cnc(client, "S-TECH-2", 1).status_code == 409
    # Every test in the approved plan needs a result.
    r = record_verify(client, "S-TECH-2", 1, results=ALL_PASSED[:1])
    assert r.status_code == 409 and "every test in the approved plan needs a result" in r.json()["detail"]
    r = record_verify(client, "S-TECH-2", 1, results=[*ALL_PASSED, {"name": "made up", "passed": True}])
    assert r.status_code == 409 and "not a test in the approved test plan" in r.json()["detail"]
    r = record_verify(client, "S-TECH-2", 1)
    body = r.json()
    assert body["passed"] is True and body["runtime_is_approved_artifact"] is True, body
    assert {x["kind"] for x in body["results"]} == {t["kind"] for t in TESTS}
    view = _view(client, "S-TECH-2")
    approval = view["packages"][0]["approval"]
    assert approval["milestone_states"] == {"apply": "applied", "build": "built", "verify": "completed",
                                           "cnc": "recorded"}
    assert [m["milestone"] for m in approval["milestones"]] == ["applied", "built", "cnc_activated", "verified"]
    assert approval["attempts"]["write"][0]["recorded"]["omw_project"] == "PRJ-JADE-1"
    # The evidence chain links every milestone to the approved package and verifies.
    events = [e.get("event") for e in entries("S-TECH-2")]
    for e in ("applied", "built", "cnc_activation_recorded", "verified"):
        assert e in events
    assert verify_chain("S-TECH-2")["valid"] is True
    actions = [h["action"] for h in view["human_actions"]]
    assert actions[:1] == ["design_approval"] and "implementation_approved" in actions and "cnc_activation" in actions
    # The story's lifecycle moves on to the as-built record.
    change = client.get("/changes/S-TECH-2", headers=headers("vdb")).json()
    assert change["lifecycle"]["nextAction"]["action"] == "finalise_asbuilt", change["lifecycle"]


def test_a_failed_build_is_recorded_and_leads_to_a_repair(client, monkeypatch):
    ready_story(client, monkeypatch, "S-TECH-FAIL")
    prepare("vdb", "S-TECH-FAIL")
    approve_package(client, "S-TECH-FAIL", 1)
    assert record_apply(client, "S-TECH-FAIL", 1).status_code == 200
    r = record_build(client, "S-TECH-FAIL", 1, succeeded=False, log="B5542001.c(12): error C2065: 'cCreditExempt'")
    assert r.status_code == 200 and r.json()["milestone"] == "build_failed", r.text
    assert "new package revision" in r.json()["next"]
    change = client.get("/changes/S-TECH-FAIL", headers=headers("vdb")).json()
    assert change["lifecycle"]["nextAction"]["action"] == "start_technical_repair"
    # The failed revision never moves on.
    assert cnc(client, "S-TECH-FAIL", 1).status_code == 409


def test_each_step_follows_the_next_milestone(client, monkeypatch):
    ready_story(client, monkeypatch, "S-TECH-ELIG")
    prepare("vdb", "S-TECH-ELIG")
    approve_package(client, "S-TECH-ELIG", 1)

    def elig():
        return _view(client, "S-TECH-ELIG")["packages"][0]["eligibility"]

    def action():
        return client.get("/changes/S-TECH-ELIG", headers=headers("vdb")).json()["lifecycle"]["nextAction"]["action"]

    assert elig() == {"eligible": True, "next_milestone": "apply", "reasons": []}
    assert action() == "record_technical_apply"
    record_apply(client, "S-TECH-ELIG", 1)
    assert elig()["next_milestone"] == "build" and elig()["eligible"] is True
    assert action() == "record_technical_build"
    record_build(client, "S-TECH-ELIG", 1)
    e = elig()
    assert e["next_milestone"] == "cnc_activation" and not e["eligible"] and "CNC operator" in e["reasons"][0]
    assert action() == "record_cnc"
    cnc(client, "S-TECH-ELIG", 1)
    assert elig() == {"eligible": True, "next_milestone": "verify", "reasons": []}
    assert action() == "record_technical_verify"


def test_only_an_application_manager_records_development_milestones(client, viewer_client, monkeypatch):
    ready_story(client, monkeypatch, "S-TECH-ROLE")
    prepare("vdb", "S-TECH-ROLE")
    approve_package(client, "S-TECH-ROLE", 1)
    assert record_apply(viewer_client, "S-TECH-ROLE", 1).status_code == 403
    assert record_apply(client, "S-TECH-ROLE", 1).status_code == 200
