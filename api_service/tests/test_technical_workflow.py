"""
The Technical workflow end to end, deterministic: a scripted Architect and a
scripted Technical Agent (stand-ins for the model) use the same run-bound
tools as the real ones; everything else is the real code -- design approval,
package storage, exact implementation approval, the governed executor and the
simulation adapter against the shared simulated estate. All approvals are by
synthetic test identities.
"""

from __future__ import annotations

from ._technical import OBJECT_KEY, ENV, approve_package, cnc, milestone, prepare, ready_story
from .conftest import headers


def _view(client, story: str, company: str = "vdb") -> dict:
    r = client.get(f"/changes/{story}/technical", headers=headers(company))
    assert r.status_code == 200, r.text
    return r.json()


def test_a_prepared_package_is_exact_inspectable_and_only_prepared(client, monkeypatch):
    art = ready_story(client, monkeypatch, "S-TECH-1")
    out = prepare("vdb", "S-TECH-1")
    assert out["submitted"] and out["revision"] == 1
    view = _view(client, "S-TECH-1")
    pkg = view["packages"][0]
    content = pkg["content"]
    assert content["design"]["design_revision"] == view["assignment"]["design_revision"]
    assert content["design"]["baseline_id"] == view["assignment"]["design_approval"]["baseline_id"]
    assert content["objects"] == [{"object_key": OBJECT_KEY, "object_name": "P554210", "object_type": "ER",
                                   "system_code": "55", "format": "jade_sim_er"}]
    source = content["sources"][0]
    assert (source["artifact_id"], source["sha256"]) == (art["artifactId"], art["sha256"])
    assert source["classification"] == "verified_active_runtime"  # simulation-only check against the estate
    cand = content["candidates"][0]
    assert cand["before_sha256"] == art["sha256"] and cand["after_sha256"] != cand["before_sha256"]
    assert '+IF BC OrderType = "SO" AND BC OrderTotal > BC CreditLimit AND BC CreditExempt != "Y" // MOD S-TECH-1' in cand["diff"]
    assert {t["kind"] for t in content["test_plan"]} == {"positive", "negative", "neighbouring"}
    assert content["toolchain"]["live_adapter"]["available"] is False
    # Prepared only: awaiting a person's exact implementation approval; nothing applied.
    assert pkg["approval"]["status"] == "pending"
    assert pkg["eligibility"]["eligible"] is False
    assert view["estate"]["objects"][OBJECT_KEY]["checked_in_sha256"] is None
    assert "SIMULATION" in view["simulation_label"] and "SYNTHETIC" in view["format_label"]
    # The original artifact is untouched.
    from jde_api_service.discovery import artifacts

    assert artifacts.get("vdb", art["artifactId"])["sha256"] == art["sha256"]


def test_the_full_simulated_lifecycle_with_separate_milestones(client, monkeypatch):
    from jde_api_service.discovery import service as discovery_service
    from jde_mcp_server import technical_sim
    from jde_mcp_server.evidence import verify_chain

    ready_story(client, monkeypatch, "S-TECH-2")
    prepare("vdb", "S-TECH-2")
    approve_package(client, "S-TECH-2", 1)
    assert milestone(client, "S-TECH-2", 1, "build").status_code == 409  # nothing applied yet
    r = milestone(client, "S-TECH-2", 1, "apply")
    assert r.status_code == 200 and r.json()["active"] is False, r.text
    obj = technical_sim.get_object("vdb", ENV, OBJECT_KEY)
    assert obj["checked_in"] and obj["active"]["package"] == "INITIAL"  # applied, not active
    r = milestone(client, "S-TECH-2", 1, "build")
    assert r.json()["milestone"] == "built", r.text
    r = milestone(client, "S-TECH-2", 1, "verify")
    assert r.status_code == 409 and "awaiting human CNC activation" in r.json()["detail"]
    r = cnc(client, "S-TECH-2", 1)
    assert r.status_code == 200 and r.json()["simulated"] is True, r.text
    r = milestone(client, "S-TECH-2", 1, "verify")
    body = r.json()
    assert body["passed"] is True and body["runtime_is_approved_artifact"] is True, body
    assert {x["kind"] for x in body["results"]} == {"positive", "negative", "neighbouring"}
    view = _view(client, "S-TECH-2")
    states = view["packages"][0]["approval"]["milestone_states"]
    assert states == {"apply": "applied", "build": "built", "verify": "completed", "cnc": "recorded"}
    milestones = [m["milestone"] for m in view["packages"][0]["approval"]["milestones"]]
    assert milestones == ["applied", "built", "cnc_activated", "verified"]
    # Subsequent discovery observes the resulting simulated state.
    grant, _ = discovery_service.grant_for_story("S-TECH-2", "vdb", agent_run_id="RUN-after", actor_user_id="u-hendro")
    ev = discovery_service.execute_read(grant, "object_librarian", "P554210",
                                        ["SIOBNM", "SIFUNO", "SISY", "SIMD", "SIPKGNAME"])
    assert ev["records"][0]["SIPKGNAME"] == "DV920TECH01" and ev["mode"] == "simulation"
    assert technical_sim.get_object("vdb", ENV, OBJECT_KEY)["active"]["sha256"] == \
        view["packages"][0]["content"]["candidates"][0]["after_sha256"]
    # The evidence chain links every milestone to the approved package and verifies.
    assert verify_chain("S-TECH-2")["valid"] is True
    actions = [h["action"] for h in view["human_actions"]]
    assert actions[:1] == ["design_approval"] and "implementation_approved" in actions and "cnc_activation" in actions
